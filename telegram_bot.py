import os
import logging
import datetime as dt
import re
import calendar
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram import __version__ as TG_VER
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & LOGGING ───────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_KEY_JSON = os.getenv("GOOGLE_KEY_JSON")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")
if GOOGLE_KEY_JSON and not os.path.exists("credentials.json"):
    with open("credentials.json", "w", encoding="utf-8") as f:
        f.write(GOOGLE_KEY_JSON)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10      # seconds for undo
MONTH_NAMES  = [
    "январь","февраль","март","апрель","май","июнь",
    "июль","август","сентябрь","октябрь","ноябрь","декабрь"
]

# PAD: две неразрывных пробелы
# Визуальные константы
SEPARATOR = "▬" * 35
PAD = "\u00A0" * 2  # Неразрывные пробелы
ICONS = {
    "high": "🚀",
    "medium": "🔥",
    "low": "⭐",
    "default": "🔸"
}

# ─── GOOGLE SHEETS I/O ──────────────────────────────────────────────────────
def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    return gspread.authorize(creds).open("TelegramBotData").sheet1

try:
    SHEET = connect_sheet()
    logging.info("Connected to Google Sheet")
except Exception as e:
    logging.error(f"Sheets connection failed: {e}")
    SHEET = None

def safe_float(s: str):
    try: return float(s.replace(",","."))
    except: return None

def sdate(d: dt.date) -> str: return d.strftime(DATE_FMT)
def pdate(s: str) -> dt.date: return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s: str) -> bool: return bool(DATE_RX.fullmatch(s.strip()))

def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx,row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row)<2: continue
        d=row[0].strip()
        if not is_date(d): continue
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        e={"date":d,"symbols":row[1].strip(),"row_idx":idx}
        if sal is not None: e["salary"]=sal
        else:                e["amount"]=amt
        key=f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def get_current_period(today: dt.date) -> tuple[dt.date, dt.date]:
    """Возвращает даты начала и конца текущего периода."""
    if today.day <= 15:
        start = today.replace(day=1)
        end = today.replace(day=15)
    else:
        start = today.replace(day=16)
        # Корректный конец месяца
        last_day = calendar.monthrange(today.year, today.month)[1]
        end = today.replace(day=last_day)
    return start, end

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row = [entry["date"], entry.get("symbols",""),
           entry.get("amount",""), entry.get("salary","")]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins=HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try:
            if pdate(v)<=nd: ins=i
            else: break
        except: continue
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx:int, symbols:str, amount:float):
    if not SHEET: return
    SHEET.update_cell(idx,2,symbols)
    SHEET.update_cell(idx,3,amount)

def delete_row(idx:int):
    if SHEET: SHEET.delete_rows(idx)

# ─── SYNC & REMINDER ────────────────────────────────────────────────────────
async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()


# ─── NAV STACK ──────────────────────────────────────────────────────────────
def init_nav(ctx):
    ctx.user_data["nav"] = deque([("main","Главное")])

def push_nav(ctx,code,label):
    ctx.user_data.setdefault("nav",deque()).append((code,label))

def pop_view(ctx):
    nav = ctx.user_data.get("nav",deque())
    if len(nav)>1: nav.pop()
    return nav[-1]

def peek_prev(ctx):
    nav = ctx.user_data.get("nav",deque())
    return nav[-2] if len(nav)>=2 else nav[-1]

# «Назад»+«Главное» для дней/месяцев/годов
def nav_kb(ctx):
    prev_code, prev_label = peek_prev(ctx)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬅️ {prev_label}", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

# только «Главное» для KPI и Истории
MAIN_ONLY_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("🏠 Главное", callback_data="main")
]])

# ─── UI & FORMAT ────────────────────────────────────────────────────────────
def fmt_amount(x: float) -> str:
    """Форматирование суммы с разделителями"""
    if abs(x - int(x)) < 1e-9:
        return f"{int(x):,}".replace(",", ".")
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    i, f = (s.split(".") if "." in s else (s, ""))
    return f"{int(i):,}".replace(",", ".") + (f and "," + f)
    
def get_amount_icon(amount: float) -> str:
    """Возвращает иконку в зависимости от суммы"""
    if amount > 2000: return ICONS["high"]
    elif amount > 1000: return ICONS["medium"]
    elif amount > 500: return ICONS["low"]
    return ICONS["default"]
    
def bounds_today():
    d=dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16)), d

def bounds_prev():
    d=dt.date.today()
    if d.day<=15:
        last = d.replace(day=1)-dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

async def safe_edit(msg:Message, text:str, kb:InlineKeyboardMarkup):
    try:    return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except: return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ─── KEYBOARDS ──────────────────────────────────────────────────────────────
def main_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{PAD*2}📅 2024{PAD*2}", callback_data="year_2024"),
            InlineKeyboardButton(f"{PAD*2}📅 2025{PAD*2}", callback_data="year_2025"),
        ],
        [ InlineKeyboardButton(f"{PAD*2}📆 Сегодня{PAD*2}", callback_data="go_today") ],
        [ InlineKeyboardButton(f"{PAD*2}➕ Запись{PAD*2}",  callback_data="add_rec") ],
        [ InlineKeyboardButton(f"{PAD*2}💰 Пришла зарплата{PAD*2}", callback_data="add_salary") ],
        [ InlineKeyboardButton(f"{PAD*3}💰 Текущая ЗП{PAD*10}", callback_data="profit_now"),
          InlineKeyboardButton(f"{PAD*3}💼 Прошлая ЗП{PAD*10}", callback_data="profit_prev") ],
        [ InlineKeyboardButton(f"{PAD*2}📜 История ЗП{PAD*2}", callback_data="hist") ],
        [ InlineKeyboardButton(f"{PAD*2}📊 KPI тек.{PAD*2}", callback_data="kpi"),
          InlineKeyboardButton(f"{PAD*2}📊 KPI прош.{PAD*2}", callback_data="kpi_prev") ],
    ])

# ─── VIEWS ─────────────────────────────────────────────────────────────────
async def show_main(msg, ctx, push=True):
    if push: init_nav(ctx)
    ctx.application.bot_data.setdefault("chats", set()).add(msg.chat_id)
    ctx.application.bot_data["entries"] = read_sheet()
    
        # Динамическая статистика (ОБНОВЛЕННЫЙ БЛОК)
    today = dt.date.today()
    start_period, end_period = get_current_period(today)
    period_entries = [
        e for e in ctx.application.bot_data["entries"].get(f"{today.year}-{today.month:02d}", [])
        if start_period <= pdate(e['date']) <= end_period
    ]
    period_total = sum(e.get('amount', 0) for e in period_entries)
    earnings_today = period_total * 0.10  # 10% от оборота периода

    # Форматирование периода
    period_label = (
        f"01–15 {MONTH_NAMES[today.month-1].capitalize()}" 
        if today.day <= 15 
        else f"16–{end_period.day} {MONTH_NAMES[today.month-1].capitalize()}"
    )

    text = f"""
{SEPARATOR}
                    🏠 <b>ГЛАВНОЕ МЕНЮ</b>
{SEPARATOR}
    
    📅 Текущий период: {period_label}
    📅 Сегодняшняя дата: {sdate(today)}
    💰 Суммарный оборот: {fmt_amount(period_total)} $
    📈 Заработок на период: {fmt_amount(earnings_today)} $
    
{SEPARATOR}
    """
    await safe_edit(msg, text, main_kb())
    
async def show_year(msg,ctx,year,push=True):
    if push: push_nav(ctx, f"year_{year}", year)
    btns = [
        InlineKeyboardButton(MONTH_NAMES[i].capitalize(), callback_data=f"mon_{year}-{i+1:02d}")
        for i in range(12)
    ]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    rows.extend(nav_kb(ctx).inline_keyboard)
    await safe_edit(msg, f"<b>{PAD*17}📆 {year}</b>", InlineKeyboardMarkup(rows))
    

async def show_month(msg,ctx,code,flag=None,push=True):
    y,m = code.split("-")
    label = f"{MONTH_NAMES[int(m)-1].capitalize()} {y}"
    if push: push_nav(ctx, f"mon_{code}", label)
    td=dt.date.today()
    if flag is None:
        flag = "old" if td.strftime("%Y-%m")==code and td.day<=15 else "new"
    ents = ctx.application.bot_data["entries"].get(code,[])
    part = [e for e in ents if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    hdr = f"""
{SEPARATOR}
                         <b>{label} • {'01–15' if flag=='old' else '16–31'}</b>
{SEPARATOR}
    """
    body = "\n".join(
        f"{PAD*8}🗓️{PAD}  {d} • {fmt_amount(sum(x['amount'] for x in part if x['date']==d))} $"
        for d in days
    ) or "Нет записей"
    ftr = f"""
{SEPARATOR}
    <b><i>✅ Итоговая сумма всех дней: {fmt_amount(total)} $</i></b>
    """
    tog = "new" if flag=="old" else "old"
    rows = [[ InlineKeyboardButton(
        "2️⃣ Вторая половина (16-31)" if flag=="old" else "1️⃣ Первая половина (01-15)",
        callback_data=f"tgl_{code}_{tog}"
    )]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.extend(nav_kb(ctx).inline_keyboard)
    await safe_edit(msg, "\n".join([hdr,body,"",ftr]), InlineKeyboardMarkup(rows))

async def show_day(msg, ctx, code, date, push=True):
    if push: 
        push_nav(ctx, f"day_{code}_{date}", date)
    
    # Обновляем данные и получаем записи
    ctx.application.bot_data["entries"] = read_sheet()
    ents = [e for e in ctx.application.bot_data["entries"].get(code, []) 
            if e["date"] == date and "amount" in e]
    
    # Форматируем заголовок
    header = f"""
{SEPARATOR}
                            🗓️ <b>{date}</b>
{SEPARATOR}
    """
    
    # Тело с иконками
    body = "\n".join(
        f"{get_amount_icon(e['amount'])} {i+1}. {e['symbols']} · {fmt_amount(e['amount'])} $"
        for i, e in enumerate(ents)
    ) or "📭 Нет записей"
    
    # Подвал с итогами
    total = sum(e["amount"] for e in ents)
    footer = f"""
{SEPARATOR}
     <b>{PAD*10}💰Итого:</b> {fmt_amount(total)} $
     <i>{PAD*9}📊 Среднее: {fmt_amount(total/len(ents)) if ents else 0} $</i>
    """
    
    # Кнопки
    rows = []
    for i, e in enumerate(ents):
        rows.append([
            InlineKeyboardButton(f"❌{i+1}", callback_data=f"confirm_del_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i+1}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ])
    rows.append([InlineKeyboardButton("➕ Запись", callback_data=f"add_{code}_{date}")])
    rows.extend(nav_kb(ctx).inline_keyboard)
    
    await safe_edit(msg, "\n".join([header, body, footer]), InlineKeyboardMarkup(rows))
    
async def show_history(msg, ctx, push=True):
    ctx.application.bot_data["entries"] = read_sheet()
    ents = [e for v in ctx.application.bot_data["entries"].values() 
            for e in v if "salary" in e]
    
    # Только основные выплаты (>=500$)
    main_entries = [e for e in ents if e["salary"] >= 500]
    penalty_entries = [e for e in ents if e["salary"] < 500]

    header = f"""
{SEPARATOR}
                 📜 <b>ИСТОРИЯ ВЫПЛАТ ЗП</b>
{SEPARATOR}"""

    lines = []
    total = 0
    
    if main_entries:
        lines.append("\n<b>Основные выплаты:</b>")
        for e in sorted(main_entries, key=lambda x: pdate(x['date'])):
            lines.append(f"{pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year} · {fmt_amount(e['salary'])} $")
            total += e['salary']
        
        lines.append(f"\n{SEPARATOR}")
        lines.append(f"<b>💰 Общая сумма: {fmt_amount(total)} $</b>")
    else:
        lines.append("\n📭 Нет данных о выплатах")

    # Кнопки в одну строку с фиксированным порядком
    keyboard = [[
        InlineKeyboardButton("⚖️ Штрафы", callback_data="penalties") if penalty_entries else None,
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]]

    # Фильтрация None-элементов
    keyboard = [[btn for btn in row if btn] for row in keyboard]

    await safe_edit(msg, header + "\n".join(lines), InlineKeyboardMarkup(keyboard))

async def show_penalties(msg, ctx):
    ents = [e for v in ctx.application.bot_data["entries"].values() 
            for e in v if "salary" in e and e["salary"] < 500]
    
    header = f"""
{SEPARATOR}
                 ⚖️ <b>ИСТОРИЯ ШТРАФОВ</b>
{SEPARATOR}"""

    lines = []
    total = 0
    
    if ents:
        for e in sorted(ents, key=lambda x: pdate(x['date'])):
            lines.append(f"{pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year} · {fmt_amount(e['salary'])} $")
            total += e['salary']
        
        lines.append(f"\n{SEPARATOR}")
        lines.append(f"<b>💰 Сумма штрафов: {fmt_amount(total)} $</b>")
    else:
        lines.append("\n📭 Нет данных о штрафах")

    # Исправленная клавиатура
    keyboard = [
        [InlineKeyboardButton("⬅️ Назад", callback_data="hist")],
        [InlineKeyboardButton("🏠 Главное", callback_data="main")]  # Убран параметр width
    ]

    await safe_edit(msg, header + "\n".join(lines), InlineKeyboardMarkup(keyboard))
    
    
async def show_profit(msg,ctx,start,end,title,push=True):
    if push: push_nav(ctx,title,title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start<=pdate(e['date'])<=end and "amount" in e]
    tot = sum(e["amount"] for e in ents)
    text = f"{title} ({sdate(start)}–{sdate(end)})\n<b>10%: {fmt_amount(tot*0.10)} $</b>"
    await safe_edit(msg, text, MAIN_ONLY_KB)

def progress_bar(progress: float) -> str:
    """Генерирует текстовый прогресс-бар"""
    bars = int(progress * 10)
    return "🟩" * bars + "⬜️" * (10 - bars)

async def show_kpi(msg, ctx, prev=False, push=True):
    # 1) Границы "исторического" периода (для prev) или "актуального" начала
    if prev:
        start, end = bounds_prev()
        title = "📊 KPI прошлого"
        period_end = end
    else:
        start_actual, today = bounds_today()
        title = "📊 KPI текущего"
        # Для расчёта filled_days отрезаем до today:
        start, end = start_actual, today
        # А для total_days и прогноза:
        y, m = start.year, start.month
        if start.day == 1:
            # первая половина
            period_end = dt.date(y, m, 15)
        else:
            # вторая — последний день месяца
            last_day = calendar.monthrange(y, m)[1]
            period_end = dt.date(y, m, last_day)

    if push and not prev:
        push_nav(ctx, title, title)

    # 2) Собираем только обороты (amount) до today (для prev — до end)
    entries = [
        e for v in ctx.application.bot_data["entries"].values() for e in v
        if start <= pdate(e["date"]) <= end and "amount" in e
    ]
    if not entries:
        return await safe_edit(msg, "Нет данных", MAIN_ONLY_KB)

    # 3) Основные метрики
    turnover = sum(e["amount"] for e in entries)
    salary   = turnover * 0.10

    # 4) Сколько **фактически** дней заполнено:
    filled_days = len({e["date"] for e in entries})

    # 5) Полное число дней в периоде для прогноза:
    total_days = (period_end - start).days + 1

    # 6) Среднее за заполненные дни:
    avg_per_day = salary / filled_days if filled_days else 0

    # 7) Прогноз:
    forecast = None if prev else avg_per_day * total_days
    
    progress = filled_days / total_days if total_days else 0
    progress_visual = f"\n{progress_bar(progress)} {int(progress*100)}%"

    # 8) Собираем текст
    header = f"{title} ({sdate(start)} – {sdate(period_end)})"
    parts = [
        f"💵 Оборот: {fmt_amount(turnover)} $",
        f"💰 Зарплата (10%): {fmt_amount(salary)} $",
        f"📆 Заполнено дней: {filled_days}/{total_days}",
        f"📈 Среднее/день: {fmt_amount(avg_per_day)} $",
        progress_visual
    ]
    
    if forecast is not None:
        parts += ["", f"<b>Прогноз на конец периода:</b> {fmt_amount(forecast)} $"]

    text = "\n".join(parts)

    # 9) Для KPI — только кнопка «Главное»
    await safe_edit(msg, f"{header}\n\n{text}", MAIN_ONLY_KB)
# ─── ADD/EDIT FLOW ──────────────────────────────────────────────────────────
async def ask_date(msg, ctx):
    """Новый дизайн ввода даты"""
    text = f"""
{SEPARATOR}
            📅 <b>ДОБАВЛЕНИЕ ЗАПИСИ</b>
{SEPARATOR}
Введите дату в формате ДД.ММ.ГГГГ 
или выберите:
    """
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Сегодня", callback_data="today_add")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ])
    
    prompt = await msg.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    ctx.user_data["flow"] = {"step": "date", "mode": "add", "msg": msg, "prompt": prompt}
    
async def ask_name(msg,ctx):
    flow = ctx.user_data["flow"]
    if flow.get("mode")=="edit":
        prompt = await msg.reply_text(f"✏️ Введите имя (старое: {flow['old_symbols']}):")
    else:
        prompt = await msg.reply_text("✏️ Введите имя:")
    flow.update({"step":"sym","prompt":prompt})

async def ask_amount(msg,ctx):
    flow = ctx.user_data["flow"]
    if flow.get("mode")=="edit":
        prev = flow["old_amount"]
        prompt = await msg.reply_text(f"💰 Введите сумму (старое: {fmt_amount(prev)} $):")
    else:
        prompt = await msg.reply_text("💰 Введите сумму:")
    flow.update({"step":"val","prompt":prompt})

async def ask_salary(msg, ctx):
    """Новый дизайн ввода зарплаты"""
    text = f"""
{SEPARATOR}
            💰 <b>ДОБАВЛЕНИЕ ЗАРПЛАТЫ</b>
{SEPARATOR}
Введите сумму зарплаты в долларах:
    """
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Сегодня", callback_data="today_salary")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ])
    
    prompt = await msg.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    ctx.user_data["flow"] = {"step": "salary", "mode": "add_salary", "msg": msg, "prompt": prompt}

# ─── ADD/EDIT FLOW ──────────────────────────────────────────────────────────
async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow:
        return

    logger.info(f"process_text step={flow['step']} mode={flow.get('mode')}")
    txt = u.message.text.strip()
    await u.message.delete()
    try:
        await flow["prompt"].delete()
    except:
        pass

    if flow["step"] == "date":
        if txt.lower() in ["сегодня", "today"]:
            flow["date"] = sdate(dt.date.today())
        elif is_date(txt):
            flow["date"] = txt
        else:
            return await flow["msg"].reply_text("Неверный формат даты. Введите дату в формате ДД.ММ.ГГГГ или слово 'сегодня'")
        
        # Вычисляем период для даты
        dt_obj = pdate(flow["date"])
        flow["period"] = f"{dt_obj.year}-{dt_obj.month:02d}"
        return await ask_name(flow["msg"], ctx)

    if flow["step"] == "sym":
        flow["symbols"] = txt
        return await ask_amount(flow["msg"], ctx)

    if flow["step"] == "val":
        try:
            val = float(txt.replace(",", "."))
        except:
            return await flow["msg"].reply_text("Нужно число")

        dt_obj = pdate(flow["date"])
        period = f"{dt_obj.year}-{dt_obj.month:02d}"
        date_str = flow["date"]
        formatted_date = f"{dt_obj.day} {MONTH_NAMES[dt_obj.month-1]} {dt_obj.year} года"

        if flow.get("mode") == "edit":
            idx = flow["row"]
            update_row(idx, flow["symbols"], val)
            ctx.application.bot_data["entries"] = read_sheet()
            
            # Сообщение с кнопкой отмены
            success_msg = await flow["msg"].reply_text(
                f"✏️ Изменено: {flow['symbols']} · {fmt_amount(val)} $\n"
                f"▫️ Дата: {formatted_date}\n"
                f"⌛ Сообщение исчезнет через {UNDO_WINDOW} секунд",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить", callback_data=f"undo_edit_{idx}")
                ]])
            )
            
            ctx.user_data["undo_edit"] = {
                "row": idx,
                "old_symbols": flow["old_symbols"],
                "old_amount": flow["old_amount"],
                "period": period,
                "date": date_str,
                "msg": flow["msg"],
                "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
            }
            
            # Автоудаление сообщения
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(success_msg.chat_id, success_msg.message_id),
                UNDO_WINDOW
            )
            ctx.user_data.pop("flow")
            return await show_day(flow["msg"], ctx, period, date_str)

        else:
            flow["amount"] = val
            row = push_row(flow)
            ctx.application.bot_data["entries"] = read_sheet()
            await show_day(flow["msg"], ctx, period, date_str)

            # Единое сообщение с кнопкой отмены
            resp = await flow["msg"].reply_text(
                f"✅ Добавлено: {flow['symbols']} · {fmt_amount(val)} $\n"
                f"▫️ Дата: {formatted_date}\n"
                f"⌛ Сообщение исчезнет через {UNDO_WINDOW} секунд",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
                ]])
            )

            ctx.user_data["undo"] = {
                "row": row,
                "msg": flow["msg"],
                "period": period,
                "date": date_str,
                "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
            }

            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(resp.chat_id, resp.message_id),
                UNDO_WINDOW
            )
            ctx.user_data.pop("flow")
            return

    if flow["step"] == "salary":
        try:
            salary_amount = float(txt.replace(",", "."))
        except:
            return await flow["msg"].reply_text("Нужно число")

        # Используем сегодняшнюю дату для зарплаты
        today = dt.date.today()
        date_str = sdate(today)
        period = f"{today.year}-{today.month:02d}"
        formatted_date = f"{today.day} {MONTH_NAMES[today.month-1]} {today.year} года"

        # Создаем запись зарплаты
        salary_entry = {
            "date": date_str,
            "symbols": "💰 Зарплата",
            "salary": salary_amount
        }
        
        row = push_row(salary_entry)
        ctx.application.bot_data["entries"] = read_sheet()

        # Сообщение об успешном добавлении
        resp = await flow["msg"].reply_text(
            f"✅ Зарплата добавлена: {fmt_amount(salary_amount)} $\n"
            f"▫️ Дата: {formatted_date}\n"
            f"⌛ Сообщение исчезнет через {UNDO_WINDOW} секунд",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
            ]])
        )

        ctx.user_data["undo"] = {
            "row": row,
            "msg": flow["msg"],
            "period": period,
            "date": date_str,
            "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
        }

        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat_id, resp.message_id),
            UNDO_WINDOW
        )
        ctx.user_data.pop("flow")
        return
# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q:
        return
    await q.answer()
    d, msg = q.data, q.message
    
    # Обработка подтверждения удаления
    if d.startswith("confirm_del_"):
        _, _, row_idx, code, date = d.split("_", 4)
        delete_row(int(row_idx))
        ctx.application.bot_data["entries"] = read_sheet()
        await msg.delete()  # Удаляем сообщение с подтверждением
        return await show_day(msg, ctx, code, date)
    
    # Остальная логика обработки callback'ов...
    if d == "add_rec":
        return await ask_date(msg, ctx)

    if d == "add_salary":
        return await ask_salary(msg, ctx)

    if d=="main":
        return await show_main(msg, ctx)

    if d=="back":
        code,label = pop_view(ctx)
        if code=="main":
            return await show_main(msg,ctx,push=False)
        if code.startswith("year_"):
            return await show_year(msg,ctx,code.split("_",1)[1],push=False)
        if code.startswith("mon_"):
            return await show_month(msg,ctx,code.split("_",1)[1],None,push=False)
        if code.startswith("day_"):
            _,c,dd = code.split("_",2)
            return await show_day(msg,ctx,c,dd,push=False)
        return await show_main(msg,ctx,push=False)

    if d=="go_today":
        ctx.application.bot_data["entries"] = read_sheet()
        td = dt.date.today()
        return await show_day(msg,ctx, f"{td.year}-{td.month:02d}", sdate(td))
    
    if d=="today_add":
        td = dt.date.today()
        ctx.user_data["flow"] = {
            "step": "sym",
            "mode": "add", 
            "date": sdate(td),
            "period": f"{td.year}-{td.month:02d}",
            "msg": msg
        }
        return await ask_name(msg, ctx)

    if d=="today_salary":
        td = dt.date.today()
        ctx.user_data["flow"] = {
            "step": "salary",
            "mode": "add_salary",
            "date": sdate(td),
            "period": f"{td.year}-{td.month:02d}",
            "msg": msg
        }
        # Пропускаем ввод даты, сразу запрашиваем сумму
        prompt = await msg.reply_text("💰 Введите сумму зарплаты:")
        ctx.user_data["flow"]["prompt"] = prompt

    if d.startswith("year_"):
        return await show_year(msg,ctx, d.split("_",1)[1])

    if d.startswith("mon_"):
        _,code = d.split("_",1)
        return await show_month(msg,ctx,code)

    if d.startswith("tgl_"):
        _,code,fl = d.split("_",2)
        return await show_month(msg,ctx,code,fl)

    if d.startswith("day_"):
        _,code,day = d.split("_",2)
        return await show_day(msg,ctx,code,day)

    if d.startswith("add_"):
        parts = d.split("_")          # ['add','2025-05','12.05.2025']
        code  = parts[1]
        date  = parts[2]
        ctx.user_data["flow"] = {
            "step":   "sym",
            "mode":   "add",
            "date":   date,
            "period": code,
            "msg":    msg
        }
        return await ask_name(msg, ctx)
    
    if d.startswith("drow_"):
        _,r,c,dd = d.split("_",4)[:4]
        delete_row(int(r))
        ctx.application.bot_data["entries"] = read_sheet()
        return await show_day(msg,ctx,c,dd)

    if d.startswith("edit_"):
        _,r,code,day = d.split("_",3)
        idx = int(r)
        old = next(e for e in ctx.application.bot_data["entries"][code] if e["row_idx"]==idx)
        ctx.user_data["flow"] = {
            "step":"sym","mode":"edit","row":idx,
            "date":day,"period":code,
            "old_symbols":old["symbols"],"old_amount":old["amount"],
            "msg":msg
        }
        return await ask_name(msg,ctx)

    if d.startswith("undo_edit_"):
        ud = ctx.user_data.get("undo_edit", {})
        parts = d.split("_")
        idx = int(parts[2])  # номер редактируемой строки
        # проверяем, что это та же самая отмена и не вышло время
        if ud.get("row") == idx and dt.datetime.utcnow() <= ud.get("expires"):
            # удаляем уведомление "✅ Изменено"
            await q.message.delete()
            # откатываем данные в таблице
            update_row(idx, ud["old_symbols"], ud["old_amount"])
            ctx.application.bot_data["entries"] = read_sheet()
            # перерисовываем исходный день в том же сообщении
            return await show_day(ud["msg"], ctx, ud["period"], ud["date"])
        else:
            return await q.message.reply_text("⏱ Время вышло")

    # ─── ОТМЕНА ДОБАВЛЕНИЯ ───────────────────────────────────────────────
    if d.startswith("undo_"):
        ud = ctx.user_data.get("undo", {})
        now = dt.datetime.utcnow()
        if ud and d == f"undo_{ud['row']}" and now <= ud["expires"]:
            # удаляем уведомление
            await msg.delete()
            # удаляем строку
            delete_row(ud["row"])
            ctx.application.bot_data["entries"] = read_sheet()
            # перерисовываем то же сообщение дня с откатанными данными
            return await show_day(ud["msg"], ctx, ud["period"], ud["date"])
        else:
            return await msg.reply_text("⏱ Время вышло")
            
    if d=="profit_now":
        s,e = bounds_today()
        return await show_profit(msg,ctx,s,e,"💰 Текущая ЗП")

    if d=="profit_prev":
        s,e = bounds_prev()
        return await show_profit(msg,ctx,s,e,"💼 Прошлая ЗП")

    if d=="hist":
        return await show_history(msg,ctx)

    if d=="kpi":
        return await show_kpi(msg,ctx,False)

    if d=="kpi_prev":
        return await show_kpi(msg,ctx,True)
        
    if d == "hist":
        return await show_history(msg, ctx)
    
    if d == "penalties":
        return await show_penalties(msg, ctx)
    
    if d == "back_to_history":
        return await show_history(msg, ctx, push=False)
        
    # В функции cb добавьте:
    elif d.startswith("cancel_del_"):
        _, _, code, date = d.split("_", 3)
        await msg.delete()  # Удаляем сообщение с подтверждением
        return await show_day(msg, ctx, code, date)

async def error_handler(update, context):
    logging.error(f"Unhandled exception {update!r}", exc_info=context.error)

async def cmd_start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data = {"entries":read_sheet(),"chats":set()}
    await update.message.reply_text(
        "📊 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_kb()
    )
    ctx.application.bot_data["chats"].add(update.effective_chat.id)

async def on_startup(app):
    # удаляем все зарегистрированные slash-команды
    await app.bot.set_my_commands([])

if __name__=="__main__":
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(on_startup)    # <-- сюда вешаем функцию очистки команд
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    # Убрано ежедневное напоминание

    logging.info("🚀 Bot up")
    app.run_polling(drop_pending_updates=True) 