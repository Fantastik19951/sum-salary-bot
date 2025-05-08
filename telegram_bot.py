import os
import logging
import datetime as dt
import re
from collections import deque, defaultdict
from dotenv import load_dotenv

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, Update
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & CREDENTIALS ───────────────────────────────────────────────────
load_dotenv()
if not os.path.exists("credentials.json"):
    creds_env = os.getenv("GOOGLE_KEY_JSON")
    if creds_env:
        with open("credentials.json", "w") as f:
            f.write(creds_env)

TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT    = "%d.%m.%Y"
DATE_RX     = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS = 4
UNDO_WINDOW = 10    # 10 секунд для отмены и удаления сообщений
REMIND_HH_MM = (20, 0)
MONTH_FULL  = ('Январь Февраль Март Апрель Май Июнь '
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
        "credentials.json", scope
    )
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
    v = (v or "").strip().replace(",", ".")
    if v in ("", "-", "—"): return None
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
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None:
            continue
        if sal is not None:
            e["salary"] = sal
        else:
            e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

def delete_row(idx):
    if SHEET:
        SHEET.delete_rows(idx)

def push_row(entry) -> int | None:
    if not SHEET:
        return None
    nd = pdate(entry["date"])
    row = [
        entry["date"],
        entry.get("symbols", ""),
        entry.get("amount", ""),
        entry.get("salary", "")
    ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i, v in enumerate(col, start=HEADER_ROWS + 1):
        try:
            d = pdate(v.strip())
        except:
            continue
        if d <= nd:
            ins = i
        else:
            break
    SHEET.insert_row(row, ins + 1, value_input_option="USER_ENTERED")
    return ins + 1

# ─── UI & NAV ---------------------------------------------------------------
def nav_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

async def safe_edit(msg, text, kb):
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def nav_push(ctx, code):
    ctx.user_data.setdefault("nav", deque(maxlen=30)).append(code)

def nav_prev(ctx):
    st: deque = ctx.user_data.get("nav", deque())
    if st:
        st.pop()
    return st.pop() if st else "main"

# ─── MAIN MENU --------------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 2024", callback_data="year_2024"),
            InlineKeyboardButton("📅 2025", callback_data="year_2025")
        ],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [
            InlineKeyboardButton("💰 Текущий заработок", callback_data="profit_now"),
            InlineKeyboardButton("💼 Прошлый заработок", callback_data="profit_prev")
        ],
        [
            InlineKeyboardButton("📊 KPI текущего", callback_data="kpi"),
            InlineKeyboardButton("📊 KPI предыдущего", callback_data="kpi_prev")
        ],
        [
            InlineKeyboardButton("➕ Запись", callback_data="add_rec"),
            InlineKeyboardButton("💵 Зарплата", callback_data="add_sal")
        ],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist")]
    ])

async def show_main(m):
    return await safe_edit(m, "📊 Главное меню", main_kb())

# ─── YEAR MENU --------------------------------------------------------------
def year_kb(year: str):
    buttons = [
        InlineKeyboardButton(f"📅 {name}", callback_data=f"mon_{year}-{i+1:02d}")
        for i, name in enumerate(MONTH_FULL)
    ]
    rows = [buttons[i:i+4] for i in range(0, 12, 4)]
    rows.extend(nav_kb().inline_keyboard)
    return InlineKeyboardMarkup(rows)

async def show_year(m, y):
    await safe_edit(m, f"📆 {y}", year_kb(y))

# ─── MONTH & DAY HELPERS ---------------------------------------------------
def half(entries, first_half: bool):
    return [e for e in entries if (pdate(e["date"]).day <= 15) == first_half]

def default_half(code: str):
    y, m = map(int, code.split("-"))
    t = dt.date.today()
    return "old" if (t.year, t.month) == (y, m) and t.day <= 15 else "new"

def crumbs_month(code, flag):
    y, m = code.split("-")
    return f"{y} · {MONTH_FULL[int(m)-1]} · {'01-15' if flag=='old' else '16-31'}"

def crumbs_day(code, date):
    y, m = code.split("-")
    return f"{y} · {MONTH_FULL[int(m)-1]} · {date}"

# ─── MONTH & DAY VIEWS ------------------------------------------------------
async def show_month(m, ctx, code, flag=None):
    flag = flag or default_half(code)
    ent = ctx.bot_data["entries"].get(code, [])
    tx = [e for e in ent if "amount" in e]
    part = half(sorted(tx, key=lambda e: pdate(e["date"])), flag == "old")
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e['amount']}" for e in part)
    # кнопки аналогично month_kb
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])
    await safe_edit(m, f"<b>{crumbs_month(code, flag)}</b>\n{body}\n\n<b>Итого:</b> {total}", kb)

async def show_day(m, ctx, code, date):
    ent = ctx.bot_data["entries"].get(code, [])
    lst = [e for e in ent if e["date"] == date and "amount" in e]
    total = sum(e["amount"] for e in lst)
    body = "\n".join(f"{e['symbols']} · {e['amount']}" for e in lst) if lst else "Записей нет"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])
    await safe_edit(m, f"<b>{crumbs_day(code, date)}</b>\n{body}\n\n<b>Итого:</b> {total}", kb)

# ─── STATISTICS, KPI, HISTORY, PROFIT --------------------------------------
# (оставляем без изменений, как в предыдущей версии)

# ─── ADD FLOW ---------------------------------------------------------------
async def ask_rec(m, ctx, target=None, mon=None):
    if target:
        ad = {"step": "sym", "date": target}
        prompt = await m.reply_text("✏️ Пожалуйста, введите имя:")
        ad["prompt_msg"] = prompt
    else:
        ad = {"step": "date"}
        inline = InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today_sel")]])
        ad["inline_msg"] = await safe_edit(m, "📅 Укажите дату (ДД.MM.ГГГГ) или нажмите «Сегодня»:", inline)
    ctx.user_data["add"] = ad

async def ask_sal(m, ctx):
    ad = {"step": "val", "mode": "salary", "date": sdate(dt.date.today())}
    prompt = await m.reply_text("💵 Пожалуйста, введите сумму:")
    ad["prompt_msg"] = prompt
    ctx.user_data["add"] = ad

async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ad = ctx.user_data.get("add")
    if not ad:
        return
    # удаляем ответ пользователя
    try: await u.message.delete()
    except: pass

    step = ad["step"]
    txt = u.message.text.strip()

    if step == "date":
        if txt and not is_date(txt):
            return await u.message.reply_text("Неверный формат, используйте ДД.MM.ГГГГ")
        ad["date"] = txt or sdate(dt.date.today())
        ad["step"] = "sym"
        # удаляем inline
        try: await ad["inline_msg"].delete()
        except: pass
        prompt = await u.message.reply_text("✏️ Пожалуйста, введите имя:")
        ad["prompt_msg"] = prompt
        return

    if step == "sym":
        ad["symbols"] = txt
        ad["step"] = "val"
        try: await ad["prompt_msg"].delete()
        except: pass
        prompt = await u.message.reply_text("💰 Пожалуйста, введите сумму:")
        ad["prompt_msg"] = prompt
        return

    if step == "val":
        try:
            val = float(txt.replace(",", "."))
        except ValueError:
            return await u.message.reply_text("Нужно ввести число")
        if ad.get("mode") == "salary":
            ad["salary"] = val
        else:
            ad["amount"] = val

        row = push_row(ad)
        ctx.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("add", None)
        try: await ad["prompt_msg"].delete()
        except: pass

        chat_id = u.effective_chat.id
        resp = await u.message.reply_html(
            f"✅ Запись добавлена:\n<b>{ad['symbols']}</b> — <b>{val}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")]])
        )
        # сохраняем отмену
        ctx.user_data["undo"] = {
            "row": row,
            "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
        }
        # удаляем подтверждение через UNDO_WINDOW сек
        ctx.application.job_queue.run_once(
            lambda jc: jc.bot.delete_message(chat_id, resp.message_id),
            when=UNDO_WINDOW
        )
        return

# ─── CALLBACK ROUTER -------------------------------------------------------
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q:
        return
    d, m = q.data, q.message
    await q.answer()

    if d.startswith("undo_"):
        row = int(d.split("_")[1])
        undo = ctx.user_data.get("undo")
        if not undo or undo["row"] != row or dt.datetime.utcnow() > undo["expires"]:
            return await m.reply_text("Срок отмены вышел")
        delete_row(row)
        ctx.bot_data["entries"] = read_sheet()
        # отправляем и удаляем сообщение об удалении через UNDO_WINDOW
        resp = await m.reply_text("🚫 Запись удалена")
        ctx.application.job_queue.run_once(
            lambda jc: jc.bot.delete_message(resp.chat_id, resp.message_id),
            when=UNDO_WINDOW
        )
        return

    if d == "today_sel":
        ad = ctx.user_data.get("add")
        if ad and ad["step"] == "date":
            ad["date"] = sdate(dt.date.today())
            ad["step"] = "sym"
            try: await ad["inline_msg"].delete()
            except: pass
            prompt = await m.reply_text("✏️ Пожалуйста, введите имя:")
            ad["prompt_msg"] = prompt
            return

    if d == "go_today":
        t = dt.date.today()
        mc, dd = f"{t.year}-{t.month:02d}", sdate(t)
        nav_push(ctx, f"day_{mc}_{dd}")
        return await show_day(m, ctx, mc, dd)

    code = d if d != "back" else nav_prev(ctx)
    if d not in ("back", "go_today"):
        nav_push(ctx, code)

    # маршруты как раньше...
    if code == "main":       return await show_main(m)
    if code == "kpi":        return await show_kpi(m, ctx)
    if code == "kpi_prev":   return await show_kpi(m, ctx, prev=True)
    if code.startswith("year_"):  return await show_year(m, code.split("_")[1])
    if code.startswith("mon_"):   return await show_month(m, ctx, code.split("_")[1])
    if code.startswith("tgl_"):
        _, mc, fl = code.split("_"); return await show_month(m, ctx, mc, fl)
    if code.startswith("stat_"):
        _, mc, fl = code.split("_"); return await show_stat(m, ctx, mc, fl)
    if code.startswith("day_"):
        _, mc, dd = code.split("_"); return await show_day(m, ctx, mc, dd)
    if code == "add_rec":    return await ask_rec(m, ctx)
    if code == "add_sal":    return await ask_sal(m, ctx)
    if code.startswith("addmon_"):
        return await ask_rec(m, ctx, mon=code.split("_")[1])
    if code.startswith("addday_"):
        _, mc, dd = code.split("_"); return await ask_rec(m, ctx, target=dd, mon=mc)
    if code == "hist":       return await show_history(m, ctx)
    if code == "profit_now":
        s, e = bounds_today(); return await show_profit(m, ctx, s, e, "💰 Текущий заработок")
    if code == "profit_prev":
        s, e = bounds_prev(); return await show_profit(m, ctx, s, e, "💼 Прошлый заработок")
    if code.startswith("drow_"):
        _, row, mc, dd = code.split("_")
        delete_row(int(row)); ctx.bot_data["entries"] = read_sheet()
        return await show_day(m, ctx, mc, dd)

# ─── START & RUN -----------------------------------------------------------
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    nav_push(ctx, "main")
    ctx.application.bot_data.setdefault("chats", set()).add(u.effective_chat.id)
    await u.message.reply_text("📊 Главное меню", reply_markup=main_kb())

# ─── SEARCH COMMAND ---------------------------------------------------------
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        return await update.message.reply_text("Использование: /search <слово или сумма>")
    ent = [e for v in context.bot_data["entries"].values() for e in v]
    if query.replace(",", ".").isdigit():
        val = float(query.replace(",", "."))
        res = [e for e in ent if e.get("amount") == val or e.get("salary") == val]
    else:
        q = query.lower()
        res = [e for e in ent if q in e["symbols"].lower()]
    if not res:
        return await update.message.reply_text("Ничего не найдено")
    res.sort(key=lambda e: pdate(e["date"]))
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary', e.get('amount'))}" for e in res)
    await update.message.reply_text(body)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data["entries"] = read_sheet()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()