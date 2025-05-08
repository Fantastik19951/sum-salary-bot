import os
import logging
import datetime as dt
import re
from collections import defaultdict
from io import BytesIO
import csv

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & LOGGING ───────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10     # seconds for undo/cancel notifications
REMIND_HH_MM = (20, 0) # daily reminder at 20:00
MONTH_FULL   = [
    "Январь","Февраль","Март","Апрель","Май","Июнь",
    "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# ─── GOOGLE SHEETS ----------------------------------------------------------
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
except Exception as e:
    logging.error(f"Sheets connection failed: {e}")
    SHEET = None

def safe_float(s):
    try: return float(s.replace(",","."))
    except: return None

def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))

def read_sheet():
    data = defaultdict(list)
    if not SHEET:
        return data
    for idx, row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2:
            continue
        d = row[0].strip()
        if not is_date(d):
            continue
        entry = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None:
            continue
        if sal is not None:
            entry["salary"] = sal
        else:
            entry["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(entry)
    return data

def push_row(entry):
    if not SHEET:
        return None
    nd = pdate(entry["date"])
    row = [
        entry["date"],
        entry.get("symbols",""),
        entry.get("amount",""),
        entry.get("salary","")
    ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try: dv = pdate(v.strip())
        except: continue
        if dv <= nd:
            ins = i
        else:
            break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def delete_row(idx):
    if SHEET:
        SHEET.delete_rows(idx)

# ─── BOT HELPERS ────────────────────────────────────────────────────────────
async def auto_sync(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data["entries"] = read_sheet()

async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    for cid in ctx.bot_data.get("chats", set()):
        try:
            await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи за сегодня!")
        except:
            pass

def nav_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

async def safe_edit(msg: Message, text: str, kb=None):
    kb = kb or nav_kb()
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def bounds_today():
    today = dt.date.today()
    start = today.replace(day=1) if today.day <= 15 else today.replace(day=16)
    return start, today

def bounds_prev():
    today = dt.date.today()
    if today.day <= 15:
        last = today.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=16), last
    else:
        return today.replace(day=1), today.replace(day=15)

# ─── MAIN MENU ──────────────────────────────────────────────────────────────
def main_kb():
    # Широкое меню: 3 колонки, 3 строки
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 2024", callback_data="year_2024"),
            InlineKeyboardButton("📅 2025", callback_data="year_2025"),
            InlineKeyboardButton("📆 Сегодня", callback_data="go_today"),
        ],
        [
            InlineKeyboardButton("➕ Запись", callback_data="add_rec"),
            InlineKeyboardButton("💵 Зарплата", callback_data="add_sal"),
            InlineKeyboardButton("↺ Отмена", callback_data="noop"),  # заглушка
        ],
        [
            InlineKeyboardButton("💰 Текущая ЗП", callback_data="profit_now"),
            InlineKeyboardButton("💼 Прошлая ЗП", callback_data="profit_prev"),
            InlineKeyboardButton("📊 KPI", callback_data="kpi"),
        ],
        [
            InlineKeyboardButton("📊 KPI Пр.", callback_data="kpi_prev"),
            InlineKeyboardButton("📜 История", callback_data="hist"),
            InlineKeyboardButton("🗄 Экспорт CSV", callback_data="export_info"),
        ]
    ])

async def show_main(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_edit(msg, "<b>📊 Главное меню</b>", main_kb())

# ─── YEAR / MONTH / DAY VIEWS ────────────────────────────────────────────────
def year_kb(year: str):
    btns = [InlineKeyboardButton(MONTH_FULL[i], callback_data=f"mon_{year}-{i+1:02d}") for i in range(12)]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back"), InlineKeyboardButton("🏠 Главное", callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_year(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, year: str):
    await safe_edit(msg, f"<b>📆 {year}</b>", year_kb(year))

def month_kb(code: str, flag: str, days: list[str]):
    togg = "old" if flag=="new" else "new"
    rows = [[InlineKeyboardButton("Первая" if flag=="new" else "Вторая", callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back"), InlineKeyboardButton("🏠 Главное", callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_month(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, code: str, flag=None):
    today = dt.date.today()
    flag = flag or ("old" if today.strftime("%Y-%m")==code and today.day<=15 else "new")
    entries = ctx.bot_data["entries"].get(code, [])
    part = [e for e in entries if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    lines = [f"{i+1}. {e['date']} · {e['symbols']} · {e['amount']}" for i,e in enumerate(part)]
    body = "\n".join(lines) or "Нет записей"
    await safe_edit(
        msg,
        f"<b>{code} · {('01–15' if flag=='old' else '16–31')}</b>\n{body}\n\n<b>Итого:</b> {total}",
        month_kb(code, flag, days)
    )

def day_kb(code: str, date: str, ents: list[dict]):
    rows = []
    for i,e in enumerate(ents, start=1):
        rows.append([
            InlineKeyboardButton(f"❌{i}", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ])
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data=f"add_{code}_{date}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back"), InlineKeyboardButton("🏠 Главное", callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_day(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, code: str, date: str):
    ents = [e for e in ctx.bot_data["entries"].get(code, []) if e["date"]==date and "amount" in e]
    total = sum(e["amount"] for e in ents)
    lines = [f"{i+1}. {e['symbols']} · {e['amount']}" for i,e in enumerate(ents, start=1)]
    body = "\n".join(lines) or "Нет записей"
    await safe_edit(
        msg,
        f"<b>{date}</b>\n{body}\n\n<b>Итого:</b> {total}",
        day_kb(code, date, ents)
    )

# ─── STAT / KPI / HISTORY / PROFIT ───────────────────────────────────────────
async def show_stat(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, code: str, flag: str):
    ents = [e for e in ctx.bot_data["entries"].get(code, []) if (pdate(e["date"]).day<=15)==(flag=="old")]
    if not ents:
        return await safe_edit(msg, "Нет данных", nav_kb())
    turn = sum(e["amount"] for e in ents)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ents})
    avg  = round(sal/days,2) if days else 0
    await safe_edit(
        msg,
        f"📊 Статистика {code}\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}\n• Ср/день: {avg}"
    )

async def show_kpi(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, prev=False):
    if prev:
        start,end = bounds_prev(); title="📊 KPI предыдущего"
    else:
        start,end = bounds_today(); title="📊 KPI текущего"
    ents = [e for v in ctx.bot_data["entries"].values() for e in v if start<=pdate(e["date"])<=end and "amount" in e]
    if not ents:
        return await safe_edit(msg, "Нет данных за период", nav_kb())
    turn = sum(e["amount"] for e in ents)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ents})
    plen = (end-start).days+1
    avg  = round(sal/days,2) if days else 0
    await safe_edit(
        msg,
        f"{title}\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}/{plen}\n• Ср/день: {avg}"
    )

async def show_history(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    ents = [e for v in ctx.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        return await safe_edit(msg, "История пуста", nav_kb())
    ents.sort(key=lambda e: pdate(e["date"]))
    total = sum(e["salary"] for e in ents)
    body = "\n".join(f"{e['date']} · {e['salary']}" for e in ents)
    await safe_edit(
        msg,
        f"📜 История ЗП\n{body}\n\n<b>Всего:</b> {total}"
    )

async def show_profit(msg: Message, ctx: ContextTypes.DEFAULT_TYPE, start, end, title: str):
    ents = [e for v in ctx.bot_data["entries"].values() for e in v if start<=pdate(e["date"])<=end and "amount" in e]
    tot = sum(e["amount"] for e in ents)
    await safe_edit(msg, f"{title}\n• 10%: {round(tot*0.10,2)}")

# ─── EXPORT & SEARCH ────────────────────────────────────────────────────────
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not re.fullmatch(r"\d{4}-\d{2}", ctx.args[0]):
        return await update.message.reply_text("Использование: /export YYYY-MM")
    code = ctx.args[0]
    ents = ctx.bot_data["entries"].get(code, [])
    if not ents:
        return await update.message.reply_text("Нет данных за этот месяц")
    buf = BytesIO(); w = csv.writer(buf)
    w.writerow(["Дата","Имя","Сумма"])
    for e in ents:
        v = e.get("amount") or e.get("salary") or 0
        w.writerow([e["date"], e["symbols"], v])
    buf.seek(0)
    await update.message.reply_document(document=buf, filename=f"export_{code}.csv")

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args).strip()
    ents = [e for v in ctx.bot_data["entries"].values() for e in v]
    # ... ваша логика поиска здесь ...
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary',e.get('amount'))}" for e in ents)
    await update.message.reply_text(body)

# ─── ADD / EDIT / UNDO FLOW ─────────────────────────────────────────────────
async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow:
        return
    txt = u.message.text.strip()
    try: await u.message.delete()
    except: pass

    if flow["step"] == "date":
        if not is_date(txt):
            return await u.message.reply_text("Формат ДД.MM.ГГГГ")
        flow["date"], flow["step"] = txt, "sym"
        return await u.message.reply_text(f"✏️ Введите имя для {flow['date']}:")
    if flow["step"] == "sym":
        flow["symbols"], flow["step"] = txt, "val"
        return await u.message.reply_text(f"💰 Введите сумму для {flow['symbols']}:")
    if flow["step"] == "val":
        try: val = float(txt.replace(",", ".")) 
        except: return await u.message.reply_text("Нужно число")
        if flow.get("mode") == "salary":
            flow["salary"] = val
        else:
            flow["amount"] = val

        row = push_row(flow)
        ctx.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("flow", None)

        # confirmation + undo button
        chat_id = u.effective_chat.id
        resp = await u.message.reply_html(
            f"✅ Запись добавлена:\n<b>{flow['symbols']}</b> — <b>{val}</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
            ]])
        )
        ctx.user_data["undo"] = {"row": row, "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)}
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(chat_id, resp.message_id),
            when=UNDO_WINDOW
        )

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q: return
    data, msg = q.data, q.message
    await q.answer()

    if data in ("main","back"):
        return await show_main(msg, ctx)
    if data.startswith("year_"):
        return await show_year(msg, ctx, data.split("_",1)[1])
    if data.startswith("mon_"):
        return await show_month(msg, ctx, data.split("_",1)[1])
    if data.startswith("tgl_"):
        _,code,fl = data.split("_",2)
        return await show_month(msg, ctx, code, fl)
    if data.startswith("day_"):
        _,code,day = data.split("_",2)
        return await show_day(msg, ctx, code, day)
    if data=="go_today":
        code = dt.date.today().strftime("%Y-%m")
        day  = sdate(dt.date.today())
        return await show_day(msg, ctx, code, day)
    if data.startswith("add_"):
        _,code,day = data.split("_",2)
        ctx.user_data["flow"] = {"step":"sym","date":day}
        return await msg.reply_text(f"✏️ Введите имя для {day}:")
    if data=="add_rec":
        ctx.user_data["flow"] = {"step":"date"}
        return await msg.reply_text("📅 Введите дату ДД.MM.ГГГГ:")
    if data=="add_sal":
        today = sdate(dt.date.today())
        ctx.user_data["flow"] = {"step":"val","mode":"salary","date":today}
        return await msg.reply_text("💵 Введите сумму зарплаты:")
    if data.startswith("drow_"):
        _,row,code,day = data.split("_",3)
        delete_row(int(row))
        ctx.bot_data["entries"] = read_sheet()
        resp = await msg.reply_text("🚫 Запись удалена")
        ctx.application.job_queue.run_once(lambda c: c.bot.delete_message(resp.chat.id, resp.message_id), when=UNDO_WINDOW)
        return await show_day(msg, ctx, code, day)
    if data.startswith("undo_"):
        row = int(data.split("_",1)[1])
        undo = ctx.user_data.get("undo", {})
        if undo.get("row")==row and dt.datetime.utcnow()<=undo.get("expires",dt.datetime.min):
            delete_row(row)
            ctx.bot_data["entries"] = read_sheet()
            try: await msg.delete()
            except: pass
            resp2 = await msg.reply_text("↺ Добавление отменено")
            ctx.application.job_queue.run_once(lambda c: c.bot.delete_message(resp2.chat.id, resp2.message_id), when=UNDO_WINDOW)
        else:
            await msg.reply_text("⏱ Срок отмены вышел")
        return
    if data.startswith("edit_"):
        _,row,code,day = data.split("_",3)
        row = int(row)
        entry = next(e for e in ctx.bot_data["entries"].get(code,[]) if e["row_idx"]==row)
        ctx.user_data["flow"] = {"step":"sym","date":day,"symbols":entry["symbols"],"mode":"edit","row":row}
        return await msg.reply_text(f"✏️ Текущее имя: {entry['symbols']}. Введите новое имя:")
    if data=="profit_now":
        s,e = bounds_today()
        return await show_profit(msg, ctx, s, e, "💰 Текущая ЗП")
    if data=="profit_prev":
        s,e = bounds_prev()
        return await show_profit(msg, ctx, s, e, "💼 Прошлая ЗП")
    if data=="kpi":
        return await show_kpi(msg, ctx, False)
    if data=="kpi_prev":
        return await show_kpi(msg, ctx, True)
    if data=="hist":
        return await show_history(msg, ctx)
    if data=="export_info":
        return await msg.reply_text("Используйте /export YYYY-MM для CSV")

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data.setdefault("chats", set()).add(update.effective_chat.id)
    ctx.bot_data["entries"] = read_sheet()
    await show_main(update.message, ctx)

if __name__=="__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data = {}
    app.bot_data["entries"] = read_sheet()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()