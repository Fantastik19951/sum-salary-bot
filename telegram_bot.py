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
    MessageHandler, ContextTypes, filters, JobQueue
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
load_dotenv()
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT    = "%d.%m.%Y"
DATE_RE     = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS = 4
UNDO_TTL    = 30  # сек для отмены последней операции

logging.basicConfig(level=logging.INFO)

# === Подключение Google Sheets ===
def connect_sheet():
    if not os.path.exists("credentials.json") and os.getenv("GOOGLE_KEY_JSON"):
        with open("credentials.json", "w") as f:
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
    logging.error(f"Google Sheets error: {e}")
    SHEET = None

# === Утилиты ===
def sdate(d: dt.date) -> str:
    return d.strftime(DATE_FMT)

def pdate(s: str) -> dt.date:
    return dt.datetime.strptime(s, DATE_FMT).date()

def is_date(s: str) -> bool:
    return bool(DATE_RE.fullmatch(s.strip()))

def safe_float(v: str):
    v = (v or "").strip().replace(",", ".")
    if v in ("", "-", "—"):
        return None
    try:
        return float(v)
    except:
        return None

# === Чтение таблицы ===
def read_sheet():
    data = defaultdict(list)
    if not SHEET:
        return data
    rows = SHEET.get_all_values()
    for idx, row in enumerate(rows, start=1):
        if idx <= HEADER_ROWS or len(row) < 2:
            continue
        d = row[0].strip()
        if not is_date(d):
            continue
        # common fields
        rec = {"date": d, "row_idx": idx}
        # salary vs amount
        sal = safe_float(row[3]) if len(row) > 3 else None
        if sal is not None:
            rec["salary"] = sal
        else:
            amt = safe_float(row[2]) if len(row) > 2 else None
            if amt is None:
                continue
            rec["amount"] = amt
            rec["symbols"] = row[1].strip()
        code = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[code].append(rec)
    return data

# === Пуш и удаление строк ===
def push_row(entry: dict) -> int | None:
    """Запись в лист:
      • обычная запись → A=date, B=symbols, C=amount
      • зарплата      → A=date, D=salary
    """
    if not SHEET:
        return None
    nd = pdate(entry["date"])
    if "salary" in entry:
        row = [entry["date"], "", "", entry["salary"]]
    else:
        row = [
            entry["date"],
            entry.get("symbols", ""),
            entry.get("amount", ""),
            ""
        ]
    colA = SHEET.col_values(1)[HEADER_ROWS:]
    pos = HEADER_ROWS
    for i, cell in enumerate(colA, start=HEADER_ROWS + 1):
        try:
            d = pdate(cell.strip())
        except:
            continue
        if d <= nd:
            pos = i
        else:
            break
    SHEET.insert_row(row, pos + 1, value_input_option="USER_ENTERED")
    return pos + 1

def delete_row(idx: int):
    if not SHEET:
        return
    SHEET.delete_rows(idx)

# === Навигационные клавиатуры ===
def nav_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="back"),
            InlineKeyboardButton("🏠 Главное", callback_data="main")
        ]
    ])

# === Главное меню ===
def build_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("💰 Текущий заработок", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлый заработок", callback_data="profit_prev")],
        [InlineKeyboardButton("📊 KPI", callback_data="kpi")],
        [InlineKeyboardButton("➕ Запись", callback_data="add_rec"),
         InlineKeyboardButton("💵 ЗП", callback_data="add_sal")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist")],
    ])

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["entries"] = read_sheet()
    await update.message.reply_text("📊 Главное меню", reply_markup=build_main())

async def safe_reply(msg, text, kb=None):
    try:
        if msg.edit_text:
            await msg.edit_text(text, reply_markup=kb)
        else:
            raise
    except:
        await msg.reply_text(text, reply_markup=kb)

# === Handlers ===

async def sync_entries(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

async def go_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # отображаем записи за сегодня
    today = dt.date.today()
    code = f"{today.year}-{today.month:02d}"
    date_str = sdate(today)
    entries = ctx.application.bot_data.setdefault("entries", read_sheet())
    today_recs = [r for r in entries.get(code, []) if r["date"] == date_str]
    text = f"📆 {date_str}\n"
    if today_recs:
        for r in today_recs:
            val = r.get("salary", r.get("amount"))
            text += f" • {r.get('symbols','ЗП')} — {val}\n"
    else:
        text += "Нет записей.\n"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить запись", callback_data="add_rec_today")],
        *nav_kb().inline_keyboard
    ])
    await safe_reply(update.callback_query or update.message, text, kb)

# -- Добавление записи --
async def add_rec(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["add"] = {"step": "date"}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📅 Сегодня", callback_data="today_sel")]])
    await safe_reply(update.callback_query, "📅 Введите дату (или выберите):", kb)

async def process_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "add" not in ctx.user_data:
        return
    ad = ctx.user_data["add"]
    txt = update.message.text.strip()
    # шаг выбора даты
    if ad["step"] == "date":
        if txt and not is_date(txt):
            return await update.message.reply_text("❗ Формат ДД.MM.YYYY")
        date = txt or sdate(dt.date.today())
        ad["date"] = date
        ad["step"] = "sym"
        return await update.message.reply_text("👤 Введите имя:", reply_markup=ReplyKeyboardRemove())
    # шаг имени
    if ad["step"] == "sym":
        ad["symbols"] = txt
        ad["step"] = "amount"
        return await update.message.reply_text("💰 Введите сумму:")
    # шаг суммы
    if ad["step"] == "amount":
        val = safe_float(txt)
        if val is None:
            return await update.message.reply_text("❗ Неверный формат суммы")
        ad["amount"] = val
        # пушим и очищаем
        row = push_row(ad)
        ctx.application.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("add")
        return await update.message.reply_text(f"✅ Добавлено: {ad['date']} | {ad['symbols']} | {val}")

# -- Добавление ЗП --
async def add_sal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    ctx.user_data["add_salary"] = {"date": sdate(dt.date.today())}
    await safe_reply(update.callback_query, "💵 Введите сумму ЗП:")

async def handle_add_salary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if "add_salary" not in ctx.user_data:
        return
    txt = update.message.text.strip()
    sal = safe_float(txt)
    if sal is None:
        return await update.message.reply_text("❗ Неверный формат суммы ЗП")
    entry = ctx.user_data.pop("add_salary")
    entry["salary"] = sal
    row = push_row(entry)
    ctx.application.bot_data["entries"] = read_sheet()
    await update.message.reply_text(f"💼 ЗП добавлена: {entry['date']} | {sal}")

# -- История ЗП --
async def hist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = ctx.application.bot_data.setdefault("entries", read_sheet())
    all_sal = []
    for m in entries.values():
        for r in m:
            if "salary" in r:
                all_sal.append(r)
    if not all_sal:
        return await update.callback_query.message.reply_text("История ЗП пуста")
    all_sal.sort(key=lambda x: pdate(x["date"]))
    text = "📜 История ЗП:\n"
    total = 0
    for r in all_sal:
        text += f" • {r['date']} — {r['salary']}\n"
        total += r['salary']
    text += f"\n<b>Всего:</b> {total}"
    await update.callback_query.message.reply_text(text, parse_mode="HTML")

# -- Синхронизация кнопкой --
async def sync_table(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"] = read_sheet()
    await update.callback_query.answer("Синхронизировано")
    await update.callback_query.message.reply_text("✅ Данные обновлены", reply_markup=build_main())

# -- Router --
async def router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = (q.data if q else None) or update.message.text
    if q:
        await q.answer()
    # маршрутизация
    if data == "main":
        return await start_cmd(update, ctx)
    if data == "go_today":
        return await go_today(update, ctx)
    if data == "add_rec":
        return await add_rec(update, ctx)
    if data == "today_sel":
        # прямая установка даты сегодня
        ctx.user_data.setdefault("add", {})["date"] = sdate(dt.date.today())
        ctx.user_data["add"]["step"] = "sym"
        return await update.callback_query.message.reply_text("👤 Введите имя:")
    if data == "add_rec_today":
        ctx.user_data["add"] = {"date": sdate(dt.date.today()), "step": "sym"}
        return await update.callback_query.message.reply_text("👤 Введите имя:")
    if data == "add_sal":
        return await add_sal(update, ctx)
    if data and update.message and not q:
        # текстовый ввод
        return await process_text(update, ctx)
    if data == "hist":
        return await hist(update, ctx)
    if data == "sync_table":
        return await sync_table(update, ctx)
    # по умолчанию — возвращаем главное
    return await start_cmd(update, ctx)

# === Запуск ===
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    # начальная загрузка
    app.bot_data["entries"] = read_sheet()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, router))

    # автосинхронизация раз в минуту
    app.job_queue.run_repeating(sync_entries, interval=60, first=10)
    logging.info("🚀 Bot started")
    app.run_polling()