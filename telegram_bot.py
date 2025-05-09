import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
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

DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10      # seconds
REMIND_HH_MM = (20, 0) # daily reminder at 20:00
MONTH_NAMES  = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря"
]

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
    logging.info("Sheets OK")
except Exception as e:
    logging.error(f"Sheets error: {e}")
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
        return data
    for idx, row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2:
            continue
        d = row[0].strip()
        if not is_date(d):
            continue
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None:
            continue
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        if sal is not None:
            e["salary"] = sal
        else:
            e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
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
        try:
            if pdate(v) <= nd:
                ins = i
            else:
                break
        except:
            continue
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx: int, symbols: str, amount: float):
    if not SHEET:
        return
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
    ctx.user_data["nav"] = deque([("main","Главное")])

def push_nav(ctx, code, label):
    ctx.user_data.setdefault("nav", deque()).append((code,label))

def pop_view(ctx):
    nav = ctx.user_data.get("nav", deque())
    if len(nav) > 1:
        nav.pop()
    return nav[-1]

def peek_prev(ctx):
    nav = ctx.user_data.get("nav", deque())
    return nav[-2] if len(nav) >= 2 else nav[-1]

def nav_kb(ctx):
    code, label = peek_prev(ctx)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬅️ {label}", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

# ─── UI HELPERS & FORMAT ────────────────────────────────────────────────────
def fmt_amount(x: float) -> str:
    if abs(x - int(x)) < 1e-9:
        return f"{int(x):,}".replace(",",".")
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    i, _, f = s.partition(".")
    return f"{int(i):,}".replace(",",".") + (f and ","+f)

def bounds_today():
    d = dt.date.today()
    return (d.replace(day=1) if d.day <= 15 else d.replace(day=16)), d

def bounds_prev():
    d = dt.date.today()
    if d.day <= 15:
        last = d.replace(day=1) - dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup):
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def main_kb():
    pad = "\u00A0"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{pad*4}📅 2024{pad*4}", callback_data="year_2024"),
         InlineKeyboardButton(f"{pad*4}📅 2025{pad*4}", callback_data="year_2025")],
        [InlineKeyboardButton(f"{pad*8}📆 Сегодня{pad*8}", callback_data="go_today")],
        [InlineKeyboardButton(f"{pad*8}➕ Запись{pad*8}", callback_data="add_rec")],
        [InlineKeyboardButton(f"{pad*8}💵 Зарплата{pad*8}", callback_data="add_sal")],
        [InlineKeyboardButton(f"{pad*6}💰 Текущая ЗП{pad*6}", callback_data="profit_now"),
         InlineKeyboardButton(f"{pad*6}💼 Прошлая ЗП{pad*6}", callback_data="profit_prev")],
        [InlineKeyboardButton(f"{pad*8}📜 История ЗП{pad*8}", callback_data="hist")],
        [InlineKeyboardButton(f"{pad*6}📊 KPI тек.{pad*6}", callback_data="kpi"),
         InlineKeyboardButton(f"{pad*6}📊 KPI прош.{pad*6}", callback_data="kpi_prev")],
    ])

# ─── VIEW FUNCTIONS ─────────────────────────────────────────────────────────
# (далее весь остальной код без изменений)

# В конце запускаем
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    # ... хендлеры, job_queue ...
    app.run_polling(drop_pending_updates=True)