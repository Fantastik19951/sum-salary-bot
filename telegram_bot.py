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
    return gspread.authorize(creds).open("TelegramBotData").sheet1

try:
    SHEET = connect_sheet()
    logging.info("✅ Google Sheets connected")
except Exception as e:
    logging.error(f"Sheets error: {e}")
    SHEET = None

# === Утилиты ===
def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RE.fullmatch(s.strip()))
def safe_float(v):
    v = (v or '').strip().replace(',', '.')
    if v in ('', '-', '—'): return None
    try: return float(v)
    except: return None

# === Чтение данных ===
def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx, row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        sal = safe_float(row[3]) if len(row) > 3 else None
        if sal is not None:
            rec = {'date': d, 'salary': sal, 'row': idx}
        else:
            amt = safe_float(row[2]) if len(row) > 2 else None
            if amt is None: continue
            rec = {'date': d, 'amount': amt, 'symbols': row[1].strip(), 'row': idx}
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(rec)
    return data

# === Запись и удаление ===
def push_row(entry):
    if not SHEET: return
    nd = pdate(entry['date'])
    if 'salary' in entry:
        row = [entry['date'], '', '', entry['salary']]
    else:
        row = [entry['date'], entry.get('symbols',''), entry.get('amount',''), '']
    colA = SHEET.col_values(1)[HEADER_ROWS:]
    pos = HEADER_ROWS
    for i, cell in enumerate(colA, start=HEADER_ROWS+1):
        try:
            d = pdate(cell)
        except:
            continue
        if d <= nd: pos = i
        else: break
    SHEET.insert_row(row, pos+1, value_input_option='USER_ENTERED')
    return pos+1

def delete_row(idx):
    if not SHEET: return
    SHEET.delete_rows(idx)

# === Клавиатуры ===
def nav_kb(): return InlineKeyboardMarkup(
    [[InlineKeyboardButton('⬅️ Назад', callback_data='back'), InlineKeyboardButton('🏠 Главное', callback_data='main')]]
)
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('📅 2024', callback_data='year_2024'), InlineKeyboardButton('📅 2025', callback_data='year_2025')],
        [InlineKeyboardButton('📆 Сегодня', callback_data='go_today')],
        [InlineKeyboardButton('💰 Текущий KPI', callback_data='kpi_now'), InlineKeyboardButton('💼 Прошлый KPI', callback_data='kpi_prev')],
        [InlineKeyboardButton('➕ Запись', callback_data='add_rec'), InlineKeyboardButton('💵 ЗП', callback_data='add_sal')],
        [InlineKeyboardButton('📜 История ЗП', callback_data='history')],
        [InlineKeyboardButton('🔄 Синхронизировать', callback_data='sync')]
    ])

async def safe_edit(msg, text, kb=None):
    try: await msg.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except: await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

# === Обработчики ===
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data['entries'] = read_sheet()
    await (update.message or update.callback_query.message).reply_text('📊 Главное меню', reply_markup=main_kb())

async def sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.bot_data['entries'] = read_sheet()
    await update.callback_query.answer('✅ Синхронизировано')
    await update.callback_query.message.reply_text('Данные обновлены', reply_markup=main_kb())

# --- Сегодня (только обычные записи) ---
async def go_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = ctx.bot_data.get('entries', read_sheet())
    today = dt.date.today(); code = f"{today.year}-{today.month:02d}"; ds = sdate(today)
    recs = [r for r in entries.get(code,[]) if r['date']==ds and 'salary' not in r]
    text = f"📆 {ds}\n" + (''.join(f"• {r['symbols']} — {r['amount']}\n" for r in recs) or 'Нет записей\n')
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('➕ Добавить', callback_data='add_rec_today')]] + nav_kb().inline_keyboard)
    await update.callback_query.message.reply_text(text, reply_markup=kb)

# --- KPI ---
def calc_kpi(records, start, end, finished):
    recs = [r for r in records if start<=pdate(r['date'])<=end and 'salary' not in r]
    total = sum(r['amount'] for r in recs)
    kpi = round(total*0.1,2)
    return total, kpi

async def show_kpi(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prev=False):
    entries = [r for vs in ctx.bot_data.get('entries', read_sheet()).values() for r in vs]
    today = dt.date.today()
    if prev:
        last = (today.replace(day=1)-dt.timedelta(days=1))
        start = last.replace(day=16) if last.day>15 else last.replace(day=1)
        end = last
        title = 'Прошлый KPI'
    else:
        start = today.replace(day=1) if today.day<=15 else today.replace(day=16)
        end = today
        title = 'Текущий KPI'
    total, kpi = calc_kpi(entries, start, end, prev)
    text = f"📊 {title}\nОборот: {total}\nKPI 10%: {kpi}"
    await update.callback_query.message.reply_text(text, reply_markup=nav_kb())

# --- История ЗП ---
async def show_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = ctx.bot_data.get('entries', read_sheet())
    sal = [r for vs in entries.values() for r in vs if 'salary' in r]
    if not sal:
        return await update.callback_query.message.reply_text('История ЗП пуста', reply_markup=nav_kb())
    sal.sort(key=lambda x:pdate(x['date']))
    text = '📜 История ЗП:\n' + ''.join(f"• {r['date']} — {r['salary']}\n" for r in sal)
    total = sum(r['salary'] for r in sal)
    text += f"\n<b>Всего:</b> {total}"
    await update.callback_query.message.reply_text(text, parse_mode='HTML', reply_markup=nav_kb())

# --- Добавление обычной записи ---
async def add_rec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['flow'] = {'step':'date','type':'rec'}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('📅 Сегодня', callback_data='sel_today')]])
    await safe_edit(update.callback_query.message, '📅 Выберите дату:', kb)

# --- Добавление записи за сегодня ---
async def add_rec_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['flow'] = {'step':'sym','date':sdate(dt.date.today()), 'type':'rec'}
    await update.callback_query.message.reply_text('👤 Введите имя:')

# --- Добавление ЗП ---
async def add_sal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data['flow'] = {'step':'salary'}
    await update.callback_query.message.reply_text('💵 Введите сумму ЗП:')

# --- Обработка текстовых сообщений в flow ---
async def process_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get('flow')
    if not flow: return
    txt = update.message.text.strip()
    # дата
    if flow['step']=='date':
        date = txt if txt and is_date(txt) else sdate(dt.date.today())
        flow['date']=date; flow['step']='sym'
        return await update.message.reply_text('👤 Введите имя:' if flow.get('type')=='rec' else '💵 Введите сумму ЗП:')
    # имя
    if flow['step']=='sym':
        flow['symbols']=txt; flow['step']='amt'
        return await update.message.reply_text('💰 Введите сумму:')
    # сумма или ЗП
    val = safe_float(txt)
    if val is None:
        return await update.message.reply_text('Неверный формат')
    entry = {'date':flow['date']}
    if flow.get('type')=='rec':
        entry.update({'symbols':flow['symbols'],'amount':val})
    else:
        entry['salary']=val
    push_row(entry)
    ctx.bot_data['entries'] = read_sheet()
    ctx.user_data.pop('flow')
    msg = '✅ Добавлено: '
    if 'salary' in entry:
        msg += f"ЗП {entry['date']} — {entry['salary']}"
    else:
        msg += f"{entry['date']} | {entry['symbols']} | {entry['amount']}"
    await update.message.reply_text(msg)

# --- Router ---
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q: d = q.data; await q.answer()
    else: d = None
    if not d and update.message:
        return await process_text(update, ctx)
    # main routes
    if d=='main': return await start(update, ctx)
    if d=='sync': return await sync(update, ctx)
    if d=='go_today': return await go_today(update, ctx)
    if d=='kpi_now': return await show_kpi(update, ctx, prev=False)
    if d=='kpi_prev': return await show_kpi(update, ctx, prev=True)
    if d=='history': return await show_history(update, ctx)
    if d=='add_rec': return await add_rec(update, ctx)
    if d=='add_rec_today': return await add_rec_today(update, ctx)
    if d=='add_sal': return await add_sal(update, ctx)
    if d=='sel_today':
        ctx.user_data['flow']={'step':'sym','date':sdate(dt.date.today()),'type':'rec'}
        return await q.message.reply_text('👤 Введите имя:')
    # fallback
    return await start(update, ctx)

# === Запуск ===
if __name__=='__main__':
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data['entries'] = read_sheet()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))
    app.run_polling()
