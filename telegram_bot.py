import os
import logging
import datetime as dt
import re
from collections import deque, defaultdict
from io import BytesIO
import csv

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# ─── CONFIG & CREDENTIALS ───────────────────────────────────────────────────
load_dotenv()
if not os.path.exists("credentials.json"):
    env = os.getenv("GOOGLE_KEY_JSON")
    if env:
        with open("credentials.json", "w") as f:
            f.write(env)

TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10     # seconds for auto-deletion of notifications
REMIND_HH_MM = (20, 0) # daily reminder at 20:00
MONTH_FULL   = [
    'Январь','Февраль','Март','Апрель','Май','Июнь',
    'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'
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
    logging.error(f"Sheets error: {e}")
    SHEET = None

# ─── HELPERS ----------------------------------------------------------------
def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))
def safe_float(v):
    v = (v or "").strip().replace(",",".")
    if v in ("","-","—"): return None
    try: return float(v)
    except: return None

# ─── SHEET I/O --------------------------------------------------------------
def read_sheet():
    data = defaultdict(list)
    if not SHEET:
        return data
    for idx, row in enumerate(SHEET.get_all_values(), 1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        if sal is not None: e["salary"] = sal
        else:             e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

async def auto_sync(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"] = read_sheet()

def delete_row(idx: int):
    if SHEET:
        SHEET.delete_rows(idx)

def push_row(entry) -> int | None:
    if not SHEET:
        return None
    nd = pdate(entry["date"])
    row = [
        entry["date"],
        entry.get("symbols",""),
        entry.get("amount",""),
        entry.get("salary",""),
    ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try: d = pdate(v.strip())
        except: continue
        if d <= nd: ins = i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

# ─── UI & NAV ---------------------------------------------------------------
def nav_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

async def safe_edit(msg, text, kb=None):
    kb = kb or nav_kb()
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ─── BOUNDS for PROFIT ------------------------------------------------------
def bounds_today():
    d = dt.date.today()
    start = d.replace(day=1) if d.day<=15 else d.replace(day=16)
    return start, d

def bounds_prev():
    today = dt.date.today()
    if today.day <= 15:
        last = today.replace(day=1) - dt.timedelta(days=1)
        start = last.replace(day=16)
        end   = last
    else:
        start = today.replace(day=1)
        end   = today.replace(day=15)
    return start, end

# ─── MAIN MENU --------------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("➕ Добавить запись", callback_data="add_rec")],
        [InlineKeyboardButton("💵 Добавить зарплату", callback_data="add_sal")],
        [InlineKeyboardButton("💰 Текущая ЗП", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлая ЗП", callback_data="profit_prev")],
        [InlineKeyboardButton("📊 KPI текущего", callback_data="kpi"),
         InlineKeyboardButton("📊 KPI предыдущего", callback_data="kpi_prev")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist"),
         InlineKeyboardButton("🗄 Экспорт CSV", callback_data="export_menu")],
    ])

async def show_main(msg, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_edit(msg, "📊 Главное меню", main_kb())

# ─── YEAR MENU --------------------------------------------------------------
def year_kb(year: str):
    buttons = [
        InlineKeyboardButton(f"📅 {MONTH_FULL[i]}", callback_data=f"mon_{year}-{i+1:02d}")
        for i in range(12)
    ]
    rows = [buttons[i:i+4] for i in range(0,12,4)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_year(msg, ctx: ContextTypes.DEFAULT_TYPE, year: str):
    await safe_edit(msg, f"📆 {year}", year_kb(year))

# ─── MONTH & DAY VIEW -------------------------------------------------------
def month_kb(code, flag, days):
    togg = "old" if flag=="new" else "new"
    rows = [[InlineKeyboardButton("Первая" if flag=="new" else "Вторая",
                                  callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_month(msg, ctx: ContextTypes.DEFAULT_TYPE, code: str, flag=None):
    flag = flag or ("old" if dt.date.today().strftime("%Y-%m")==code and dt.date.today().day<=15 else "new")
    ent = ctx.application.bot_data["entries"].get(code, [])
    tx = [e for e in ent if "amount" in e]
    part = [e for e in tx if (pdate(e["date"]).day<=15)==(flag=="old")]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e['amount']}" for e in part) or "Нет записей"
    await safe_edit(msg,
        f"<b>{code} · {('01–15' if flag=='old' else '16–31')}</b>\n{body}\n\n<b>Итого:</b> {total}",
        month_kb(code, flag, days)
    )

def day_kb(code, date, lst):
    rows = [[InlineKeyboardButton("❌", callback_data=f"drow_{e['row_idx']}_{code}_{date}")] for e in lst]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_day(msg, ctx: ContextTypes.DEFAULT_TYPE, code: str, date: str):
    lst = [
        e for e in ctx.application.bot_data["entries"].get(code, [])
        if e["date"]==date and "amount" in e
    ]
    total = sum(e["amount"] for e in lst)
    body = "\n".join(f"{e['symbols']} · {e['amount']}" for e in lst) or "Нет записей"
    await safe_edit(msg,
        f"<b>{date}</b>\n{body}\n\n<b>Итого:</b> {total}",
        day_kb(code, date, lst)
    )

# ─── STATISTICS, KPI, HISTORY, PROFIT --------------------------------------
async def show_stat(msg, ctx: ContextTypes.DEFAULT_TYPE, code: str, flag: str):
    ent = [
        e for e in ctx.application.bot_data["entries"].get(code, [])
        if (pdate(e["date"]).day<=15)==(flag=="old")
    ]
    if not ent:
        return await safe_edit(msg, "Нет данных", nav_kb())
    turn = sum(e.get("amount",0) for e in ent)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ent})
    avg  = round(sal/days,2) if days else 0
    await safe_edit(msg,
        f"📊 Статистика {code}\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}\n• Среднее: {avg}"
    )

async def show_kpi(msg, ctx: ContextTypes.DEFAULT_TYPE, prev=False):
    if prev:
        start,end = bounds_prev(); title="📊 KPI предыдущего"
    else:
        start,end = bounds_today(); title="📊 KPI текущего"
    ent = [
        e for v in ctx.application.bot_data["entries"].values() for e in v
        if start<=pdate(e["date"])<=end and "amount" in e
    ]
    if not ent:
        return await safe_edit(msg, "Нет данных за период", nav_kb())
    turn = sum(e["amount"] for e in ent)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ent})
    plen = (end - start).days + 1
    avg  = round(sal/days,2) if days else 0
    await safe_edit(msg,
        f"{title}\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}/{plen}\n• Среднее: {avg}"
    )

async def show_history(msg, ctx: ContextTypes.DEFAULT_TYPE):
    ent = [
        e for v in ctx.application.bot_data["entries"].values() for e in v
        if "salary" in e
    ]
    if not ent:
        return await safe_edit(msg, "История пуста", nav_kb())
    ent.sort(key=lambda e: pdate(e["date"]))
    total = sum(e["salary"] for e in ent)
    body = "\n".join(f"{e['date']} · {e['salary']}" for e in ent)
    await safe_edit(msg,
        f"📜 История ЗП\n{body}\n\n<b>Всего:</b> {total}"
    )

async def show_profit(msg, ctx: ContextTypes.DEFAULT_TYPE, start, end, title: str):
    ent = [
        e for v in ctx.application.bot_data["entries"].values() for e in v
        if start<=pdate(e["date"])<=end and "amount" in e
    ]
    tot = sum(e["amount"] for e in ent)
    await safe_edit(msg,
        f"{title}\n• 10%: {round(tot*0.10,2)}"
    )

# ─── EXPORT CSV -------------------------------------------------------------
async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not re.fullmatch(r"\d{4}-\d{2}", ctx.args[0]):
        return await update.message.reply_text("Использование: /export YYYY-MM")
    code = ctx.args[0]
    ent  = ctx.application.bot_data["entries"].get(code, [])
    if not ent:
        return await update.message.reply_text("Нет данных за этот месяц")
    buf = BytesIO()
    w = csv.writer(buf)
    w.writerow(["Дата","Имя","Сумма"])
    for e in ent:
        v = e.get("amount") or e.get("salary") or 0
        w.writerow([e["date"], e["symbols"], v])
    buf.seek(0)
    await update.message.reply_document(document=buf, filename=f"export_{code}.csv")

# ─── SEARCH ---------------------------------------------------------------
async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args).strip()
    ent = [e for v in ctx.application.bot_data["entries"].values() for e in v]
    res = []
    m = re.match(r"^(\d{2}\.\d{2}\.\d{4})-(\d{2}\.\d{2}\.\d{4})$", q)
    if m:
        d1,d2 = map(pdate, m.groups())
        res = [e for e in ent if d1<=pdate(e["date"])<=d2]
    else:
        m2 = re.match(r"^([<>])\s*(\d+)$", q)
        if m2:
            op,val = m2.group(1), float(m2.group(2))
            if op==">":
                res = [e for e in ent if (e.get("amount") or e.get("salary") or 0)>val]
            else:
                res = [e for e in ent if (e.get("amount") or e.get("salary") or 0)<val]
        else:
            res = [e for e in ent if q.lower() in e["symbols"].lower()]
    if not res:
        return await update.message.reply_text("Ничего не найдено")
    res.sort(key=lambda e: pdate(e["date"]))
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary',e.get('amount'))}" for e in res)
    await update.message.reply_text(body)

# ─── ADD FLOW ---------------------------------------------------------------
async def ask_rec(msg, ctx: ContextTypes.DEFAULT_TYPE, target=None):
    if target:
        ctx.user_data["add"] = {"step":"sym","date":target}
        return await msg.reply_text(f"✏️ Сегодня ({target}) — введите имя:")
    ctx.user_data["add"] = {"step":"date"}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="go_today")]])
    await safe_edit(msg, "📅 Укажите дату или нажмите «Сегодня»:", kb)

async def ask_sal(msg, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add"] = {"step":"val","mode":"salary","date":sdate(dt.date.today())}
    await msg.reply_text("💵 Введите сумму:")

async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ad = ctx.user_data.get("add")
    if not ad:
        return
    txt = u.message.text.strip()
    try: await u.message.delete()
    except: pass

    if ad["step"] == "date":
        if txt and not is_date(txt):
            return await u.message.reply_text("Формат ДД.MM.ГГГГ")
        ad["date"] = txt or sdate(dt.date.today())
        ad["step"] = "sym"
        return await u.message.reply_text(f"✏️ Введите имя для {ad['date']}:")
    if ad["step"] == "sym":
        ad["symbols"] = txt
        ad["step"] = "val"
        return await u.message.reply_text(f"💰 Введите сумму для {ad['symbols']}:")
    if ad["step"] == "val":
        try: val = float(txt.replace(",",".")) 
        except:
            return await u.message.reply_text("Нужно число")
        if ad.get("mode")=="salary":
            ad["salary"] = val
        else:
            ad["amount"] = val
        row = push_row(ad)
        ctx.application.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("add", None)
        resp = await u.message.reply_html(
            f"✅ Запись добавлена:\n<b>{ad['symbols']}</b> — <b>{val}</b>"
        )
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id, resp.message_id),
            when=UNDO_WINDOW
        )

# ─── CALLBACK ROUTER --------------------------------------------------------
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q: return
    d, m = q.data, q.message
    await q.answer()

    if d == "main":
        return await show_main(m, ctx)
    if d == "back":
        return await show_main(m, ctx)

    if d.startswith("year_"):
        year = d.split("_",1)[1]
        return await show_year(m, ctx, year)
    if d.startswith("mon_"):
        code = d.split("_",1)[1]
        return await show_month(m, ctx, code)
    if d.startswith("tgl_"):
        _,code,fl = d.split("_",2)
        return await show_month(m, ctx, code, fl)
    if d.startswith("day_"):
        _,code,dd = d.split("_",2)
        return await show_day(m, ctx, code, dd)

    if d == "go_today":
        today = sdate(dt.date.today())
        return await ask_rec(m, ctx, target=today)

    if d.startswith("drow_"):
        _,row,code,day = d.split("_",3)
        delete_row(int(row))
        ctx.application.bot_data["entries"] = read_sheet()
        resp = await m.reply_text("🚫 Запись удалена")
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id, resp.message_id),
            when=UNDO_WINDOW
        )
        return await show_day(m, ctx, code, day)

    if d == "profit_now":
        s,e = bounds_today()
        return await show_profit(m, ctx, s, e, "💰 Текущая ЗП")
    if d == "profit_prev":
        s,e = bounds_prev()
        return await show_profit(m, ctx, s, e, "💼 Прошлая ЗП")

    if d == "kpi":
        return await show_kpi(m, ctx, False)
    if d == "kpi_prev":
        return await show_kpi(m, ctx, True)

    if d == "hist":
        return await show_history(m, ctx)

    if d == "export_menu":
        return await m.reply_text("Используйте /export YYYY-MM для выгрузки CSV")

    if d == "add_rec":
        return await ask_rec(m, ctx)
    if d == "add_sal":
        return await ask_sal(m, ctx)

# ─── REMINDER ---------------------------------------------------------------
async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    for cid in ctx.application.bot_data.get("chats", set()):
        try:
            await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи за сегодня!")
        except Exception as e:
            logging.warning(f"reminder error: {e}")

# ─── START & RUN ------------------------------------------------------------
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data.setdefault("chats", set()).add(u.effective_chat.id)
    await show_main(u.message, ctx)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
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