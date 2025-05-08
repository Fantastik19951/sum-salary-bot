import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 30
MONTH_FULL   = ('Январь Февраль Март Апрель Май Июнь '
                'Июль Август Сентябрь Октябрь Ноябрь Декабрь').split()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# ─── GOOGLE SHEETS ----------------------------------------------------------
def connect_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive.file",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope)
    return gspread.authorize(creds).open("TelegramBotData").sheet1

try:
    SHEET = connect_sheet()
    logging.info("✅ Google Sheets подключён")
except Exception as e:
    logging.error(f"Sheets error: {e}")
    SHEET = None

# ─── HELPERS ----------------------------------------------------------------
def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))
def safe_float(v):
    v = (v or '').strip().replace(',', '.')
    if v in ('', '-', '—'): return None
    try: return float(v)
    except: return None

# ─── SHEET I/O --------------------------------------------------------------
def read_sheet():
    data = defaultdict(list)
    if not SHEET:
        return data
    for idx, row in enumerate(SHEET.get_all_values(), 1):
        if idx <= HEADER_ROWS or len(row) < 2:
            continue
        d = row[0].strip()
        if not is_date(d):
            continue
        rec = {'date': d, 'symbols': row[1].strip(), 'row_idx': idx}
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None:
            continue
        # Если 'salary' есть, мы его сохраняем в rec['salary'], иначе rec['amount']
        if sal is not None:
            rec['salary'] = sal
        else:
            rec['amount'] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(rec)
    return data

# ─── UI & NAV ---------------------------------------------------------------
def nav_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back"),
         InlineKeyboardButton("🏠 Главное", callback_data="main")]
    ])

async def safe_edit(msg, text, kb):
    try:
        await msg.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except:
        await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

def nav_push(ctx, code):
    ctx.user_data.setdefault("nav", deque(maxlen=50)).append(code)

def nav_prev(ctx):
    stack = ctx.user_data.get("nav", deque())
    if stack:
        stack.pop()
    return stack.pop() if stack else "main"

# ─── MENUS -----------------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("💰 Текущий заработок", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлый заработок", callback_data="profit_prev")],
        [InlineKeyboardButton("📊 KPI текущего", callback_data="kpi"),
         InlineKeyboardButton("📊 KPI предыдущего", callback_data="kpi_prev")],
        [InlineKeyboardButton("➕ Добавить запись", callback_data="add_rec"),
         InlineKeyboardButton("💵 Добавить ЗП", callback_data="add_sal")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist")]
    ])

def year_kb(year):
    rows = []
    for i in range(0, 12, 4):
        rows.append([
            InlineKeyboardButton(f"📅 {MONTH_FULL[i]}", callback_data=f"mon_{year}-{i+1:02d}"),
            InlineKeyboardButton(f"📅 {MONTH_FULL[i+1]}", callback_data=f"mon_{year}-{i+2:02d}"),
            InlineKeyboardButton(f"📅 {MONTH_FULL[i+2]}", callback_data=f"mon_{year}-{i+3:02d}"),
            InlineKeyboardButton(f"📅 {MONTH_FULL[i+3]}", callback_data=f"mon_{year}-{i+4:02d}"),
        ])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

# ─── HANDLERS --------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("entries", {})
    await (update.message or update.callback_query.message).reply_text(
        "📊 Добро пожаловать!", reply_markup=main_kb()
    )

async def show_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    year = query.data.split("_")[1]
    await query.answer()
    await show_menu := query.message.edit_text
    await show_menu(f"📆 Выберите месяц {year}", reply_markup=year_kb(year))

# === Новая реализация show_month (без ЗП!) ===
async def show_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split("_")[1]  # формат YYYY-MM
    entries = context.user_data.setdefault("entries", {}).get(code, [])
    # Фильтруем только те записи, где нет ключа 'salary'
    daily = [e for e in entries if 'salary' not in e]
    if not daily:
        return await query.message.reply_text("Записей за этот месяц без ЗП нет.", reply_markup=nav_kb())

    total = sum(e.get('amount', 0) for e in daily)
    text = f"<b>📂 {code}</b>\n"
    for e in daily:
        text += f"{e['date']} | {e['symbols']} | {e.get('amount', 0)}\n"
    text += f"\n<b>Итого:</b> {total}"
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=nav_kb())

# === Новая реализация show_day (без ЗП!) ===
async def show_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, code, date = query.data.split("_")  # формат day_{YYYY-MM}_{DD.MM.YYYY}
    entries = context.user_data.setdefault("entries", {}).get(code, [])
    # Фильтруем только обычные записи
    daily = [e for e in entries if e['date'] == date and 'salary' not in e]
    if not daily:
        return await query.message.reply_text("Записей за этот день без ЗП нет.", reply_markup=nav_kb())

    total = sum(e.get('amount', 0) for e in daily)
    text = f"<b>📋 {date}</b>\n"
    for e in daily:
        text += f"{e['symbols']} | {e.get('amount', 0)}\n"
    text += f"\n<b>Итого:</b> {total}"
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=nav_kb())

# остальные хендлеры оставляем без изменения...

# ─── ROUTING ---------------------------------------------------------------
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    data = q.data
    if data.startswith("year_"):
        return await show_year(update, context)
    if data.startswith("mon_"):
        return await show_month(update, context)
    if data.startswith("day_"):
        return await show_day(update, context)
    # ... другие routes ...

# ─── MAIN ---------------------------------------------------------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(router))
    # ... добавьте остальные хендлеры ...
    logging.info("Запускаем бота...")
    app.run_polling()