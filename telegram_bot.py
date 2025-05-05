import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
load_dotenv()
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT    = "%d.%m.%Y"
DATE_RE     = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS = 4
UNDO_TTL    = 30
MONTHS_FULL = [
    'Январь','Февраль','Март','Апрель','Май','Июнь',
    'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'
]
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# === Google Sheets ===
def connect_sheet():
    if not os.path.exists("credentials.json") and os.getenv("GOOGLE_KEY_JSON"):
        with open("credentials.json","w") as f:
            f.write(os.getenv("GOOGLE_KEY_JSON"))
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    return client.open("TelegramBotData").sheet1

try:
    SHEET = connect_sheet()
    logging.info("✅ Google Sheets connected")
except Exception as e:
    logging.error(f"Sheets error: {e}")
    SHEET = None

# === Helpers ===
def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RE.fullmatch(s.strip()))
def safe_float(v):
    v = (v or '').strip().replace(',', '.')
    if v in ('', '-', '—'): return None
    try: return float(v)
    except: return None

# === Data I/O ===

def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx, row in enumerate(SHEET.get_all_values(), 1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        rec = { 'date': d, 'row': idx }
        sal = safe_float(row[3]) if len(row)>3 else None
        if sal is not None:
            rec['salary'] = sal
        else:
            amt = safe_float(row[2]) if len(row)>2 else None
            if amt is None: continue
            rec['amount'] = amt
            rec['symbols'] = row[1].strip()
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(rec)
    return data

# === Write/Delete ===
def push_row(entry):
    if not SHEET: return
    nd = pdate(entry['date'])
    if 'salary' in entry:
        row = [entry['date'],'','', entry['salary']]
    else:
        row = [entry['date'], entry.get('symbols',''), entry.get('amount',''),'']
    colA = SHEET.col_values(1)[HEADER_ROWS:]
    pos = HEADER_ROWS
    for i, v in enumerate(colA, start=HEADER_ROWS+1):
        try: d = pdate(v)
        except: continue
        if d <= nd: pos = i
        else: break
    SHEET.insert_row(row, pos+1, value_input_option='USER_ENTERED')
    return pos+1

def delete_row(idx):
    if not SHEET: return
    SHEET.delete_rows(idx)

# === Keyboards ===
def nav_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⬅️ Назад', callback_data='back'),
         InlineKeyboardButton('🏠 Главное', callback_data='main')]
    ])

# === Menus ===
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📅 2024', callback_data='year_2024'),
         InlineKeyboardButton('📅 2025', callback_data='year_2025')],
        [InlineKeyboardButton('📆 Сегодня', callback_data='go_today')],
        [InlineKeyboardButton('💰 Текущий KPI', callback_data='kpi_now'),
         InlineKeyboardButton('💼 Прошлый KPI', callback_data='kpi_prev')],
        [InlineKeyboardButton('➕ Запись', callback_data='add_rec'),
         InlineKeyboardButton('💵 ЗП', callback_data='add_sal')],
        [InlineKeyboardButton('📜 История ЗП', callback_data='history')],
        [InlineKeyboardButton('🔄 Синхронизировать', callback_data='sync')]
    ])

# === Handlers ===
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data['entries'] = read_sheet()
    await (update.message or update.callback_query.message).reply_text(
        '📊 Главное меню', reply_markup=main_kb()
    )

async def sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data['entries'] = read_sheet()
    await update.callback_query.answer('✅ Синхронизировано')
    await update.callback_query.message.reply_text('Данные обновлены', reply_markup=main_kb())

async def go_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = ctx.bot_data.get('entries', read_sheet())
    today = dt.date.today(); code = f"{today.year}-{today.month:02d}"; ds = sdate(today)
    recs = [r for r in entries.get(code,[]) if r['date']==ds]
    text = f"📆 {ds}\n" + ("".join(f"• {r.get('symbols','ЗП')} — {r.get('salary',r.get('amount'))}\n" for r in recs) or 'Нет записей')
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ Добавить запись', callback_data='add_rec_today')],
        *nav_kb().inline_keyboard
    ])
    await update.callback_query.message.reply_text(text, reply_markup=kb)

# KPI
async def show_kpi(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prev: bool=False):
    entries = ctx.bot_data.get('entries', read_sheet())
    t = dt.date.today()
    if prev:
        end = (t.replace(day=1)-dt.timedelta(days=1))
        start = end.replace(day=1 if end.day>15 else 16)
        label = 'Прошлый KPI'
    else:
        start = t.replace(day=1 if t.day<=15 else 16)
        end = t
        label = 'Текущий KPI'
    recs = [r for vs in entries.values() for r in vs if start<=pdate(r['date'])<=end]
    turnover = sum(r.get('amount',0) for r in recs)
    kpi = round(turnover*0.1,2)
    await update.callback_query.message.reply_text(
        f"📊 {label}:\nОборот — {turnover}\nKPI 10% — {kpi}",
        reply_markup=nav_kb()
    )

# History
async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = ctx.bot_data.get('entries', read_sheet())
    sal = [r for vs in entries.values() for r in vs if 'salary' in r]
    if not sal:
        return await update.callback_query.message.reply_text('История ЗП пуста', reply_markup=nav_kb())
    sal.sort(key=lambda r:pdate(r['date']))
    text = '📜 История ЗП:\n' + ''.join(f"• {r['date']} — {r['salary']}\n" for r in sal)
    total = sum(r['salary'] for r in sal)
    text += f"\n<b>Всего:</b> {total}"
    await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=nav_kb())

# Add regular rec
async def add_rec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['flow'] = {'step':'date','type':'rec'}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('📅 Сегодня', callback_data='sel_today')]])
    await update.callback_query.message.reply_text('📅 Выберите дату:', reply_markup=kb)

# Add salary
async def add_sal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['flow'] = {'step':'salary'}
    await update.callback_query.message.reply_text('💵 Введите сумму ЗП:')

# Process text
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get('flow')
    if not flow: return
    txt = update.message.text.strip()
    if flow['step']=='date':
        date = txt if txt and is_date(txt) else sdate(dt.date.today())
        flow['date']=date; flow['step']='sym'
        return await update.message.reply_text('👤 Введите имя:')
    if flow['step']=='sym':
        flow['symbols']=txt; flow['step']='amt'
        return await update.message.reply_text('💰 Введите сумму:')
    if flow['step']=='amt':
        amt = safe_float(txt)
        if amt is None: return await update.message.reply_text('Неверный формат')
        entry={'date':flow['date'],'symbols':flow['symbols'],'amount':amt}
        row = push_row(entry)
        ctx.bot_data['entries']=read_sheet()
        ctx.user_data.pop('flow')
        return await update.message.reply_text(f"✅ Добавлено: {entry['date']} | {entry['symbols']} | {amt}")
    if flow['step']=='salary':
        sal = safe_float(txt)
        if sal is None: return await update.message.reply_text('Неверный формат')
        entry={'date':sdate(dt.date.today()),'salary':sal}
        push_row(entry)
        ctx.bot_data['entries']=read_sheet()
        ctx.user_data.pop('flow')
        return await update.message.reply_text(f"💼 ЗП добавлена: {entry['date']} | {sal}")

# Router
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data if q else None
    if q: await q.answer()
    if not d and update.message: return await text_handler(update, ctx)
    # main
    if d=='main': return await start(update, ctx)
    if d=='sync': return await sync(update, ctx)
    if d=='go_today': return await go_today(update, ctx)
    if d=='kpi_now': return await show_kpi(update, ctx, prev=False)
    if d=='kpi_prev': return await show_kpi(update, ctx, prev=True)
    if d=='history': return await history(update, ctx)
    if d=='add_rec': return await add_rec(update, ctx)
    if d=='add_sal': return await add_sal(update, ctx)
    if d=='sel_today':
        ctx.user_data['flow']={'step':'sym','date':sdate(dt.date.today())}
        return await q.message.reply_text('👤 Введите имя:')
    # fallback to main
    return await start(update, ctx)

# === Run ===
if __name__=='__main__':
    app=ApplicationBuilder().token(TOKEN).build()
    app.bot_data['entries']=read_sheet()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))
    app.run_polling()
