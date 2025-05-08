import os, logging, datetime as dt, re
from collections import deque, defaultdict
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
REMIND_HH_MM = (20, 0)
MONTH_FULL   = ('Январь','Февраль','Март','Апрель','Май','Июнь',
                'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')

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
    v = (v or '').strip().replace(',', '.')
    if v in ('', '-', '—'): return None
    try: return float(v)
    except: return None

# ─── SHEET I/O --------------------------------------------------------------
def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx, row in enumerate(SHEET.get_all_values(), 1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        entry = {'date': d, 'symbols': row[1].strip(), 'row_idx': idx}
        if sal is not None:
            entry['salary'] = sal
        else:
            entry['amount'] = amt
        data[f"{pdate(d).year}-{pdate(d).month:02d}"].append(entry)
    return data

async def auto_sync(ctx): ctx.application.bot_data["entries"] = read_sheet()
def delete_row(idx): SHEET and SHEET.delete_rows(idx)

def push_row(entry) -> int | None:
    if not SHEET: return None
    nd = pdate(entry['date'])
    row = [entry['date'], entry.get('symbols',''),
           entry.get('amount',''), entry.get('salary','')]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i, v in enumerate(col, start=HEADER_ROWS+1):
        try: d = pdate(v.strip())
        except: continue
        if d <= nd: ins = i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

# ─── UI & NAV ---------------------------------------------------------------
def nav_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back"),
         InlineKeyboardButton("🏠 Главное", callback_data="main")]
    ])

async def safe_edit(msg, text, kb):
    try:    await msg.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except: await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

def nav_push(ctx, code):
    ctx.user_data.setdefault("nav", deque(maxlen=30)).append(code)

def nav_prev(ctx):
    st = ctx.user_data.get("nav", deque())
    if st: st.pop()
    return st.pop() if st else "main"

# ─── MAIN MENU --------------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("💰 Текущий заработок", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлый заработок", callback_data="profit_prev")],
        [InlineKeyboardButton("➕ Запись", callback_data="add_rec"),
         InlineKeyboardButton("💵 Зарплата", callback_data="add_sal")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist")]
    ])

async def show_main(m): await safe_edit(m, "📊 Главное меню", main_kb())

# ─── YEAR MENU --------------------------------------------------------------
def year_kb(y):
    buttons = [InlineKeyboardButton(MONTH_FULL[i],
                  callback_data=f"month_{y}-{i+1:02d}") for i in range(12)]
    kb = [buttons[i:i+2] for i in range(0,12,2)]
    kb.append([InlineKeyboardButton("↩️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(kb)

async def show_year(m, ctx):
    year = ctx.split('_')[1]
    await safe_edit(m, f"📆 Месяцы {year}", year_kb(year))

# ─── MONTH & DAY helpers ----------------------------------------------------
def half(entries, first_half):
    # НЕ фильтруем здесь, salary уберём только при показе
    return [e for e in entries if (pdate(e['date']).day <=15)==first_half]

def crumbs_month(code, flag):
    y,m = code.split('-')
    return f"{y} · {MONTH_FULL[int(m)-1]} · {'01-15' if flag=='old' else '16-31'}"

# ─── MONTH VIEW -------------------------------------------------------------
async def show_month(m, ctx):
    code = ctx.split('_')[1]
    flag = ctx.split('_')[-1] if ctx.startswith('tgl_') else None
    entries = ctx.application.bot_data["entries"].get(code, [])
    # тут исключаем salary
    if flag is None:
        flag = 'new' if dt.date.today().day>15 else 'old'
    part = [e for e in half(entries, flag=='old') if 'salary' not in e]
    days = sorted({e['date'] for e in part}, key=pdate)
    total = sum(e['amount'] for e in part)
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e['amount']}" for e in part) or "Записей нет"
    kb = month_kb(code, flag, days)
    await safe_edit(m, f"<b>{crumbs_month(code,flag)}</b>\n{body}\n\n<b>Итого:</b> {total}", kb)

# ─── DAY VIEW ---------------------------------------------------------------
async def show_day(m, ctx):
    _,code,date = ctx.split('_')
    entries = ctx.application.bot_data["entries"].get(code, [])
    part = [e for e in entries if e['date']==date and 'salary' not in e]
    total = sum(e['amount'] for e in part)
    body = "\n".join(f"{e['symbols']} · {e['amount']}" for e in part) or "Записей нет"
    kb = day_kb(code, date, part)
    await safe_edit(m, f"<b>{date}</b>\n{body}\n\n<b>Итого:</b> {total}", kb)

# ─── HISTORY ZP -------------------------------------------------------------
async def show_history(m, ctx):
    lst = [e for v in ctx.application.bot_data["entries"].values() for e in v if 'salary' in e]
    lst.sort(key=lambda e:pdate(e['date']))
    body = "\n".join(f"{e['date']} · {e['salary']}" for e in lst) or "История пуста"
    total = sum(e['salary'] for e in lst)
    await safe_edit(m, f"<b>📜 История ЗП</b>\n{body}\n\n<b>Всего:</b> {total}", nav_kb())

# … остальной cb-router, add flows и запуск без изменений  
# просто убедись, что show_month, show_day и show_history зарегистрированы у тебя корректно:

if __name__=="__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data["entries"] = read_sheet()
    app.add_handler(CommandHandler("start", show_main))
    app.add_handler(CallbackQueryHandler(auto_sync, pattern="^sync_table$"))
    app.add_handler(CallbackQueryHandler(show_year, pattern="^year_"))
    app.add_handler(CallbackQueryHandler(show_month, pattern="^month_"))
    app.add_handler(CallbackQueryHandler(show_day, pattern="^day_"))
    app.add_handler(CallbackQueryHandler(show_history, pattern="^hist$"))
    app.job_queue.run_repeating(auto_sync, interval=10, first=10)
    hh,mm = REMIND_HH_MM
    app.job_queue.run_daily(auto_sync, time=dt.time(hour=hh, minute=mm))
    logging.info("🚀 Bot up")
    app.run_polling()