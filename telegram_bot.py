import os
import logging
import datetime as dt
import re
from collections import deque, defaultdict
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
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
REMIND_HH_MM = (20, 0)
MONTH_FULL   = ('Январь Февраль Март Апрель Май Июнь '
                'Июль Август Сентябрь Октябрь Ноябрь Декабрь').split()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ─── Google Sheets ----------------------------------------------------------
def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive"
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
    v = (v or '').replace(',', '.').strip()
    try: return float(v)
    except: return None

# ─── I/O --------------------------------------------------------------------
def read_sheet():
    data = defaultdict(list)
    if not SHEET:
        return data
    rows = SHEET.get_all_values()
    for idx, row in enumerate(rows, start=1):
        if idx <= HEADER_ROWS or len(row) < 2:
            continue
        date = row[0].strip()
        if not is_date(date):
            continue
        name = row[1].strip()
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None:
            continue
        entry = {'date': date, 'symbols': name, 'row': idx}
        # сохраняем оба, но при выводе будем фильтровать
        if sal is not None:
            entry['salary'] = sal
        else:
            entry['amount'] = amt
        code = f"{pdate(date).year}-{pdate(date).month:02d}"
        data[code].append(entry)
    return data

async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

def push_row(entry):
    if not SHEET:
        return
    nd = pdate(entry['date'])
    row = [
        entry['date'],
        entry.get('symbols', ''),
        entry.get('amount',''),
        entry.get('salary',''),
    ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i, v in enumerate(col, start=HEADER_ROWS+1):
        try:
            d = pdate(v.strip())
        except:
            continue
        if d <= nd:
            ins = i
        else:
            break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def delete_row(idx):
    if SHEET:
        SHEET.delete_rows(idx)

# ─── UI и навигация ---------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📆 Сегодня",   callback_data="today")],
        [InlineKeyboardButton("📅 По месяцу", callback_data="month_menu")],
        [InlineKeyboardButton("📊 KPI",       callback_data="kpi_menu")],
        [InlineKeyboardButton("📜 История ЗП",callback_data="history")],
    ])

def month_kb(year):
    buttons = [InlineKeyboardButton(MONTH_FULL[i], callback_data=f"month_{year}-{i+1:02d}")
               for i in range(12)]
    kb = [buttons[i:i+3] for i in range(0,12,3)]
    kb.append([InlineKeyboardButton("↩️ Главное", callback_data="main")])
    return InlineKeyboardMarkup(kb)

def kpi_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Текущий период", callback_data="kpi_current")],
        [InlineKeyboardButton("Прошлый период",  callback_data="kpi_prev")],
        [InlineKeyboardButton("↩️ Главное",       callback_data="main")],
    ])

# ─── Обработчики ------------------------------------------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"] = read_sheet()
    await update.message.reply_text("Главное меню", reply_markup=main_kb())

async def show_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.application.bot_data["entries"] = read_sheet()
    await update.callback_query.message.edit_text("Главное меню", reply_markup=main_kb())

async def show_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    entries = [e for lst in ctx.application.bot_data["entries"].values() for e in lst
               if e['date']==sdate(dt.date.today()) and 'salary' not in e]
    if not entries:
        text = "Записей нет"
    else:
        text = "\n".join(f"{e['symbols']} — {e['amount']}" for e in entries)
    await update.callback_query.message.edit_text(f"📆 Сегодня:\n{text}", reply_markup=main_kb())

async def show_month_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    year = dt.date.today().year
    await update.callback_query.message.edit_text(f"📅 Выберите месяц {year}", reply_markup=month_kb(year))

async def show_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    code = update.callback_query.data.split('_')[1]
    entries = [e for e in ctx.application.bot_data["entries"].get(code, []) if 'salary' not in e]
    if not entries:
        text = "Записей нет"
    else:
        text = "\n".join(f"{e['date']} {e['symbols']} — {e['amount']}" for e in entries)
    await update.callback_query.message.edit_text(f"📅 {code}\n{text}", reply_markup=main_kb())

async def show_kpi_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.message.edit_text("📊 KPI", reply_markup=kpi_kb())

async def show_kpi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    data = ctx.application.bot_data["entries"]
    today = dt.date.today()
    if update.callback_query.data == "kpi_current":
        start = today.replace(day=1)
        end = today
    else:
        prev = today.replace(day=1) - dt.timedelta(days=1)
        start = prev.replace(day=1)
        end = prev
    recs = [e for lst in data.values() for e in lst
            if start<=pdate(e['date'])<=end and 'salary' not in e]
    turn = sum(e['amount'] for e in recs)
    sal10= turn*0.10
    days = len({e['date'] for e in recs}) or 1
    avg  = sal10/days
    fin  = end < today
    forecast = sal10 if fin else avg*((end-start).days+1)
    text = (
        f"Оборот: {turn:.2f}\n"
        f"Заработок 10%: {sal10:.2f}\n"
        f"Дней: {days}\n"
        f"Среднее/день: {avg:.2f}\n"
        f"Прогноз: {forecast:.2f}"
    )
    await update.callback_query.message.edit_text(text, reply_markup=main_kb())

async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    sal = [e for lst in ctx.application.bot_data["entries"].values() for e in lst if 'salary' in e]
    if not sal:
        text = "История пуста"
    else:
        text = "\n".join(f"{e['date']} — {e['salary']}" for e in sal)
    await update.callback_query.message.edit_text(f"📜 История ЗП\n{text}", reply_markup=main_kb())

# ─── RUN --------------------------------------------------------------------
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(show_main,       pattern="^main$"))
    app.add_handler(CallbackQueryHandler(show_today,      pattern="^today$"))
    app.add_handler(CallbackQueryHandler(show_month_menu, pattern="^month_menu$"))
    app.add_handler(CallbackQueryHandler(show_month,      pattern="^month_[0-9]{4}-[0-9]{2}$"))
    app.add_handler(CallbackQueryHandler(show_kpi_menu,   pattern="^kpi_menu$"))
    app.add_handler(CallbackQueryHandler(show_kpi,        pattern="^kpi_(current|prev)$"))
    app.add_handler(CallbackQueryHandler(show_history,    pattern="^history$"))
    app.job_queue.run_repeating(auto_sync, interval=30, first=10)
    hh,mm = REMIND_HH_MM
    app.job_queue.run_daily(auto_sync, time=dt.time(hour=hh, minute=mm))
    logging.info("🚀 Bot started")
    app.run_polling()