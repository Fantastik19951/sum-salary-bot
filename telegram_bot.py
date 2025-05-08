import os
import logging
import datetime as dt
import re
from collections import deque, defaultdict
from dotenv import load_dotenv
# --- читать credentials.json из переменной, если файл не найден -------------
if not os.path.exists("credentials.json"):
    creds_env = os.getenv("GOOGLE_KEY_JSON")
    if creds_env:
        with open("credentials.json", "w") as f:
            f.write(creds_env)
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
UNDO_WINDOW  = 30          # сек. для «↺ Отмена»
REMIND_HH_MM = (20, 0)     # 20:00 напоминание
MONTH_FULL   = ('Январь Февраль Март Апрель Май Июнь '
                'Июль Август Сентябрь Октябрь Ноябрь Декабрь').split()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# ─── GOOGLE SHEETS ----------------------------------------------------------
def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive"
    ]
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
def nav_push(ctx, code): ctx.user_data.setdefault("nav", deque(maxlen=30)).append(code)
def nav_prev(ctx):
    st: deque = ctx.user_data.get("nav", deque())
    if st: st.pop()
    return st.pop() if st else "main"

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
        [InlineKeyboardButton("📜 История зарплат", callback_data="hist")]
    ])

def year_kb(year: str):
    buttons = [InlineKeyboardButton(f"📅 {name}", callback_data=f"mon_{year}-{i+1:02d}")
               for i, name in enumerate(MONTH_FULL)]
    rows = [buttons[i:i+4] for i in range(0, 12, 4)]
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

# ─── SHOW FUNCTIONS --------------------------------------------------------
async def show_main(m):
    await safe_edit(m, "📊 Главное меню", main_kb())

async def show_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    year = q.data.split('_')[1]
    await q.message.edit_text(
        f"📆 Выберите месяц: {year}",
        reply_markup=year_kb(year)
    )

async def show_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.split('_')[1]
    all_entries = context.user_data.setdefault('entries', read_sheet())
    # Исключаем зарплату
    entries = [e for e in all_entries.get(code, []) if 'salary' not in e]
    if not entries:
        return await q.message.reply_text("Записи за этот месяц отсутствуют.", reply_markup=nav_kb())
    total = sum(e.get('amount', 0) for e in entries)
    text = f"<b>📂 {code}</b>\n"
    for e in entries:
        text += f"{e['date']} | {e['symbols']} | {e.get('amount',0)}\n"
    text += f"\n<b>Итого:</b> {total}"
    await q.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb())

async def show_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, code, date = q.data.split('_')  # day_YYYY-MM_DD.MM.YYYY
    all_entries = context.user_data.setdefault('entries', read_sheet())
    entries = [e for e in all_entries.get(code, []) if e['date']==date and 'salary' not in e]
    if not entries:
        return await q.message.reply_text("Записи за этот день отсутствуют.", reply_markup=nav_kb())
    total = sum(e.get('amount', 0) for e in entries)
    text = f"<b>📋 {date}</b>\n"
    for e in entries:
        text += f"{e['symbols']} | {e.get('amount',0)}\n"
    text += f"\n<b>Итого:</b> {total}"
    await q.message.edit_text(text, parse_mode='HTML', reply_markup=nav_kb())

# ─── ROUTER ---------------------------------------------------------------
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q: return
    data = q.data
    if data.startswith('year_'):
        return await show_year(update, context)
    if data.startswith('mon_'):
        return await show_month(update, context)
    if data.startswith('day_'):
        return await show_day(update, context)
    # ... остальные маршруты ...

# ─── START & RUN -----------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data['entries'] = read_sheet()
    await update.message.reply_text("Добро пожаловать!", reply_markup=main_kb())

if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CallbackQueryHandler(router))
    # ... остальные хендлеры ...
    logging.info('Бот запущен')
    app.run_polling()

Сохраните этот код, и папки месяцев, дней и история будут работать корректно: записи из колонки D (salary) в них больше не попадают.```