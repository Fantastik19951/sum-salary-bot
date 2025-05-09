import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & LOGGING ───────────────────────────────────────────────────────
load_dotenv()
# 1) Проверяем наличие credentials.json
if not os.path.exists("credentials.json"):
    logging.error("„credentials.json“ не найден! Проверьте GOOGLE_KEY_JSON в окружении.")
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10      # seconds
REMIND_HH_MM = (20, 0) # daily at 20:00
MONTH_NAMES  = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря"
]
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

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
    rows = SHEET.get_all_values()
    logging.info(f"SHEET подключён — всего строк (включая заголовки): {len(rows)}")
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
    if not SHEET:
        logging.warning("read_sheet: SHEET is None, возвращаю пустой словарь")
        return data

    rows = SHEET.get_all_values()
    for idx,row in enumerate(rows, start=1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None: continue
        entry = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        if sal is not None: entry["salary"] = sal
        else:             entry["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(entry)

    # 2) Логируем количество периодов и записей
    periods = len(data)
    entries = sum(len(v) for v in data.values())
    logging.info(f"read_sheet: найдено {periods} периодов, всего записей: {entries}")
    return data

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row = [ entry["date"],
            entry.get("symbols",""),
            entry.get("amount",""),
            entry.get("salary","") ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try:
            if pdate(v) <= nd: ins = i
            else: break
        except:
            continue
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx: int, symbols: str, amount: float):
    if not SHEET: return
    SHEET.update_cell(idx, 2, symbols)
    SHEET.update_cell(idx, 3, amount)

def delete_row(idx: int):
    if SHEET:
        SHEET.delete_rows(idx)

# ─── SYNC & REMINDER ────────────────────────────────────────────────────────
async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

async def reminder(ctx):
    for cid in ctx.application.bot_data.get("chats", set()):
        try:
            await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи сегодня!")
        except:
            pass

# ─── NAVIGATION STACK ───────────────────────────────────────────────────────
def init_nav(ctx):
    ctx.user_data["nav"] = [("main","Главное меню")]

def push_nav(ctx, code, label):
    ctx.user_data.setdefault("nav", []).append((code, label))

def pop_view(ctx):
    nav = ctx.user_data.get("nav", [])
    if len(nav) > 1: nav.pop()
    return nav[-1]

def peek_prev(ctx):
    nav = ctx.user_data.get("nav", [])
    return nav[-2] if len(nav)>=2 else nav[-1]

def nav_kb(ctx):
    prev_code, prev_label = peek_prev(ctx)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬅️ {prev_label}", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

# ─── UI & FORMAT ────────────────────────────────────────────────────────────
def main_kb():
    pad = "\u00A0"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{pad*4}📅 2024{pad*4}", "year_2024"),
         InlineKeyboardButton(f"{pad*4}📅 2025{pad*4}", "year_2025")],
        [InlineKeyboardButton(f"{pad*8}📆 Сегодня{pad*8}", "go_today")],
        [InlineKeyboardButton(f"{pad*8}➕ Запись{pad*8}", "add_rec")],
        [InlineKeyboardButton(f"{pad*8}💵 Зарплата{pad*8}", "add_sal")],
        [InlineKeyboardButton(f"{pad*6}💰 Текущая ЗП{pad*6}", "profit_now"),
         InlineKeyboardButton(f"{pad*6}💼 Прошлая ЗП{pad*6}", "profit_prev")],
        [InlineKeyboardButton(f"{pad*8}📜 История ЗП{pad*8}", "hist")],
        [InlineKeyboardButton(f"{pad*6}📊 KPI тек.{pad*6}", "kpi"),
         InlineKeyboardButton(f"{pad*6}📊 KPI прош.{pad*6}", "kpi_prev")],
    ])

async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup):
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def fmt_amount(x: float) -> str:
    if abs(x - int(x)) < 1e-9:
        return f"{int(x):,}".replace(",",".")
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    i,_,f = s.partition(".")
    return f"{int(i):,}".replace(",",".") + (f and ","+f)

def bounds_today():
    d = dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16), d)

def bounds_prev():
    d = dt.date.today()
    if d.day<=15:
        last = d.replace(day=1) - dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

# ─── VIEW FUNCTIONS ─────────────────────────────────────────────────────────
async def show_main(msg, ctx, push=True):
    if push: init_nav(ctx)
    ctx.application.bot_data.setdefault("chats", set()).add(msg.chat_id)
    ctx.application.bot_data["entries"] = read_sheet()
    await safe_edit(msg, "📊 <b>Главное меню</b>", main_kb())

# ... весь остальной код show_year, show_month, show_day, flow и cb без изменений ...

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data = {"entries": read_sheet(), "chats": set()}

    # 3) Initial load лог
    logging.info(f"Initial load: периоды={len(ctx.application.bot_data['entries'])}, чаты={len(ctx.application.bot_data['chats'])}")

    await show_main(update.message, ctx)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    # сразу прогружаем data
    app.bot_data["entries"] = read_sheet()
    app.bot_data["chats"] = set()
    logging.info(f"App start load: периоды={len(app.bot_data['entries'])}, чаты={len(app.bot_data['chats'])}")

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()