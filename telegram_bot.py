import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update, Message
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
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
logger = logging.getLogger(__name__)

DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10      # seconds
REMIND_HH_MM = (20, 0) # daily reminder
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
    logger.info("Connected to Google Sheet")
except Exception as e:
    logger.error(f"Sheets connection failed: {e}")
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
    for idx,row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None: continue
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        if sal is not None: e["salary"] = sal
        else:              e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def push_row(entry):
    if not SHEET: return None
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
            if pdate(v) <= nd: ins = i
            else: break
        except:
            continue
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx:int, symbols:str, amount:float):
    if not SHEET: return
    SHEET.update_cell(idx, 2, symbols)
    SHEET.update_cell(idx, 3, amount)

def delete_row(idx:int):
    if SHEET:
        SHEET.delete_rows(idx)

# ─── SYNC & REMINDERS ────────────────────────────────────────────────────────
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
    ctx.user_data.setdefault("nav", deque()).append((code, label))

def peek_prev(ctx):
    nav = ctx.user_data.get("nav", deque())
    return nav[-2] if len(nav) >= 2 else nav[-1]

def pop_view(ctx):
    nav = ctx.user_data.get("nav", deque())
    if len(nav) > 1:
        nav.pop()
    return nav[-1]

def nav_main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное", callback_data="main")]])

def nav_kb(ctx):
    c, l = peek_prev(ctx)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⬅️ {l}", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

# ─── UI & FORMAT ────────────────────────────────────────────────────────────
def fmt_amount(x: float) -> str:
    if abs(x - int(x)) < 1e-9:
        return f"{int(x):,}".replace(",",".")
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    i, _, f = s.partition(".")
    i_fmt = f"{int(i):,}".replace(",",".")
    return i_fmt + (f and ","+f)

def bounds_today():
    d = dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16)), d

def bounds_prev():
    d = dt.date.today()
    if d.day <= 15:
        last = d.replace(day=1) - dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

async def safe_edit(msg: Message, text: str, kb=None):
    kb = kb or nav_main_kb()
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def main_kb():
    PAD = "\u00A0"*2
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{PAD}📅 2024{PAD}", callback_data="year_2024"),
         InlineKeyboardButton(f"{PAD}📅 2025{PAD}", callback_data="year_2025")],
        [InlineKeyboardButton(f"{PAD}📆 Сегодня{PAD}", callback_data="go_today")],
        [InlineKeyboardButton(f"{PAD}➕ Запись{PAD}", callback_data="add_rec")],
        [InlineKeyboardButton(f"{PAD}💵 Зарплата{PAD}", callback_data="add_sal")],
        [InlineKeyboardButton(f"{PAD*5}💰 Текущая ЗП{PAD*10}", callback_data="profit_now"),
         InlineKeyboardButton(f"{PAD*5}💼 Прошлая ЗП{PAD*10}", callback_data="profit_prev")],
        [InlineKeyboardButton(f"{PAD}📜 История ЗП{PAD}", callback_data="hist")],
        [InlineKeyboardButton(f"{PAD}📊 KPI тек.{PAD}", callback_data="kpi"),
         InlineKeyboardButton(f"{PAD}📊 KPI прош.{PAD}", callback_data="kpi_prev")],
    ])

# ─── VIEWS ──────────────────────────────────────────────────────────────────
async def show_main(msg, ctx, push=True):
    if push: init_nav(ctx)
    ctx.application.bot_data.setdefault("chats", set()).add(msg.chat.id)
    ctx.application.bot_data["entries"] = read_sheet()
    text = "📊 <b>Главное меню</b>"
    await safe_edit(msg, text, main_kb())

async def show_year(msg, ctx, year, push=True):
    if push: push_nav(ctx, f"year_{year}", year)
    btns = [InlineKeyboardButton(MONTH_NAMES[i].capitalize(),
             callback_data=f"mon_{year}-{i+1:02d}") for i in range(12)]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    rows.extend(nav_kb(ctx).inline_keyboard)
    await safe_edit(msg, f"<b>📆 {year}</b>", InlineKeyboardMarkup(rows))

async def show_month(msg, ctx, code, flag=None, push=True):
    y,m = code.split("-")
    label = f"{MONTH_NAMES[int(m)-1].capitalize()} {y}"
    if push: push_nav(ctx, f"mon_{code}", label)
    td = dt.date.today()
    if flag is None:
        flag = "old" if td.strftime("%Y-%m")==code and td.day<=15 else "new"
    ents = ctx.application.bot_data["entries"].get(code, [])
    part = [e for e in ents if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    header = f"<b>{label} · {'01–15' if flag=='old' else '16–31'}</b>"
    body = "\n".join(f"{d} · {fmt_amount(sum(e['amount'] for e in part if e['date']==d))} $" for d in days) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"
    tog = "new" if flag=="old" else "old"
    rows = [[InlineKeyboardButton(
        "Первая половина" if flag=="old" else "Вторая половина",
        callback_data=f"tgl_{code}_{tog}"
    )]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.extend(nav_kb(ctx).inline_keyboard)
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_day(msg, ctx, code, date, push=True):
    if push: push_nav(ctx, f"day_{code}_{date}", date)
    ctx.application.bot_data["entries"] = read_sheet()
    ents = [e for e in ctx.application.bot_data["entries"].get(code, []) if e["date"]==date and "amount" in e]
    total = sum(e["amount"] for e in ents)
    header = f"<b>{date}</b>"
    body = "\n".join(f"{i+1}. {e['symbols']} · {fmt_amount(e['amount'])} $" for i,e in enumerate(ents)) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"
    rows = []
    for i,e in enumerate(ents):
        rows.append([
            InlineKeyboardButton(f"❌{i+1}", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i+1}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ])
    rows.append([InlineKeyboardButton("➕ Запись", callback_data=f"add_{code}_{date}")])
    rows.extend(nav_kb(ctx).inline_keyboard)
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_history(msg, ctx, push=True):
    if push: push_nav(ctx, "hist", "История ЗП")
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        text = "История пуста"
    else:
        lines = [
            f"• {pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year} — {fmt_amount(e['salary'])} $"
            for e in sorted(ents, key=lambda x: pdate(x['date']))
        ]
        text = "<b>📜 История ЗП</b>\n" + "\n".join(lines)
    await safe_edit(msg, text, nav_kb(ctx))

async def show_profit(msg, ctx, start, end, title, push=True):
    if push: push_nav(ctx, title, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start <= pdate(e['date']) <= end and "amount" in e]
    tot = sum(e["amount"] for e in ents)
    text = f"{title} ({sdate(start)} – {sdate(end)})\n<b>10%: {fmt_amount(tot*0.10)} $</b>"
    await safe_edit(msg, text, nav_kb(ctx))

async def show_kpi(msg, ctx, prev: bool):
    # переработанная логика прогноза
    if prev:
        start, end = bounds_prev()
        title = "📊 KPI прошлого"
    else:
        start, end = bounds_today()
        title = "📊 KPI текущего"

    ents = [
        e for v in ctx.application.bot_data["entries"].values() for e in v
        if start <= pdate(e['date']) <= end and "amount" in e
    ]
    if not ents:
        return await safe_edit(msg, "<b>Нет данных</b>", nav_main_kb())

    total_turn   = sum(e["amount"] for e in ents)
    total_sal    = total_turn * 0.10
    filled_days  = len({pdate(e['date']) for e in ents})
    avg_per_day  = total_sal / filled_days if filled_days else 0
    period_len   = 15
    if prev:
        forecast = total_sal
    else:
        forecast = round(avg_per_day * period_len, 2)

    text = (
        f"<b>{title} ({sdate(start)} – {sdate(end)})</b>\n"
        f"• Оборот: {fmt_amount(total_turn)} $\n"
        f"• ЗП10%: {fmt_amount(total_sal)} $\n"
        f"• Дней: {filled_days}/{period_len}\n"
        f"• Ср/день: {fmt_amount(avg_per_day)} $\n"
        f"• Прогноз: {fmt_amount(forecast)} $"
    )
    await safe_edit(msg, text, nav_main_kb())

# ─── ADD / EDIT FLOW ─────────────────────────────────────────────────────────
async def ask_date(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = await msg.reply_text(
        "📅 Введите дату (ДД.MM.ГГГГ) или «Сегодня»",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Сегодня", callback_data="go_today")
        ]])
    )
    ctx.user_data["flow"] = {"step":"date", "msg":msg, "prompt":prompt}

async def ask_name(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data["flow"]
    if flow.get("mode") == "edit":
        prompt = await msg.reply_text(f"✏️ Введите имя (старое: {flow['old_symbols']}):")
    else:
        prompt = await msg.reply_text("✏️ Введите имя:")
    flow.update({"step":"sym", "prompt":prompt})

async def ask_amount(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data["flow"]
    if flow.get("mode") == "edit":
        prev = flow["old_amount"]
        prompt = await msg.reply_text(f"💰 Введите сумму (старое: {fmt_amount(prev)} $):")
    else:
        prompt = await msg.reply_text("💰 Введите сумму:")
    flow.update({"step":"val", "prompt":prompt})

async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow:
        return

    txt = u.message.text.strip()
    await u.message.delete()
    try:
        await flow["prompt"].delete()
    except:
        pass

    if flow["step"] == "date":
        if txt.lower() == "сегодня":
            flow["date"] = sdate(dt.date.today())
        elif is_date(txt):
            flow["date"] = txt
        else:
            return await flow["msg"].reply_text("❗ Неверный формат даты")
        return await ask_name(flow["msg"], ctx)

    if flow["step"] == "sym":
        flow["symbols"] = txt
        return await ask_amount(flow["msg"], ctx)

    if flow["step"] == "val":
        try:
            val = float(txt.replace(",","."))
        except:
            return await flow["msg"].reply_text("❗ Нужно число")
        date_str = flow["date"]
        period = date_str[:7]

        # EDIT
        if flow.get("mode") == "edit":
            idx = flow["row"]
            update_row(idx, flow["symbols"], val)
            ctx.application.bot_data["entries"] = read_sheet()

            resp = await flow["msg"].reply_text(
                "✅ Запись обновлена",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить", callback_data=f"undo_edit_{idx}")
                ]])
            )
            ctx.user_data["undo_edit"] = {
                "row": idx,
                "old_symbols": flow["old_symbols"],
                "old_amount": flow["old_amount"],
                "period": period,
                "date": date_str,
                "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
            }
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(resp.chat.id, resp.message_id),
                when=UNDO_WINDOW
            )
            ctx.user_data.pop("flow", None)
            return await show_day(flow["msg"], ctx, period, date_str)

        # ADD
        row = push_row({
            "date": flow["date"],
            "symbols": flow["symbols"],
            "amount": val
        })
        ctx.application.bot_data["entries"] = read_sheet()

        resp = await flow["msg"].reply_text(
            f"✅ Добавлено: {flow['symbols']} · {fmt_amount(val)} $",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
            ]])
        )
        ctx.user_data["undo"] = {
            "row": row,
            "period": period,
            "date": date_str,
            "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
        }
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id, resp.message_id),
            when=UNDO_WINDOW
        )
        ctx.user_data.pop("flow", None)
        return await show_day(flow["msg"], ctx, period, date_str)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q:
        return
    await q.answer()
    d, msg = q.data, q.message

    # ADD flows
    if d == "add_rec":
        return await ask_date(msg, ctx)
    if d.startswith("add_"):
        _, period, date = d.split("_", 2)
        ctx.user_data["flow"] = {
            "step":"sym", "mode":"add",
            "date":date, "period":period,
            "msg":msg
        }
        return await ask_name(msg, ctx)
    if d == "add_sal":
        today = sdate(dt.date.today())
        period = today[:7]
        ctx.user_data["flow"] = {
            "step":"val", "mode":"salary",
            "date":today, "period":period,
            "msg":msg
        }
        return await ask_amount(msg, ctx)

    # go today
    if d == "go_today":
    # 1) обновляем данные
    ctx.application.bot_data["entries"] = read_sheet()
    today = sdate(dt.date.today())
    period = today[:7]
    # 2) если мы пришли из "add_rec→Сегодня", то запускаем диалог
    if ctx.user_data.get("flow", {}).get("step") == "date":
        return await ask_name(msg, ctx)
    # 3) иначе — обычный просмотр
    return await show_day(msg, ctx, period, today)
    # undo
    if d.startswith("undo_"):
        idx = int(d.split("_",1)[1])
        ud = ctx.user_data.get("undo", {})
        if ud.get("row")==idx and dt.datetime.utcnow()<=ud.get("expires"):
            delete_row(idx)
            ctx.application.bot_data["entries"] = read_sheet()
            return await show_day(msg, ctx, ud["period"], ud["date"])
        return await msg.reply_text("⏱ Время вышло")
    if d.startswith("undo_edit_"):
        idx = int(d.split("_",1)[1])
        ud = ctx.user_data.get("undo_edit", {})
        if ud.get("row")==idx and dt.datetime.utcnow()<=ud.get("expires"):
            update_row(idx, ud["old_symbols"], ud["old_amount"])
            ctx.application.bot_data["entries"] = read_sheet()
            return await show_day(msg, ctx, ud["period"], ud["date"])
        return await msg.reply_text("⏱ Время вышло")

    # edit
    if d.startswith("edit_"):
        _, r, period, date = d.split("_",3)
        idx = int(r)
        old = next(e for e in ctx.application.bot_data["entries"][period] if e["row_idx"]==idx)
        ctx.user_data["flow"] = {
            "step":"sym", "mode":"edit",
            "row":idx, "period":period, "date":date,
            "old_symbols":old["symbols"],
            "old_amount":old.get("amount") or old.get("salary"),
            "msg":msg
        }
        return await ask_name(msg, ctx)

    # navigation
    if d == "main":
        return await show_main(msg, ctx)
    if d == "back":
        code,label = pop_view(ctx)
        if code=="main":
            return await show_main(msg, ctx, push=False)
        if code.startswith("year_"):
            return await show_year(msg, ctx, code.split("_",1)[1], push=False)
        if code.startswith("mon_"):
            return await show_month(msg, ctx, code.split("_",1)[1], None, push=False)
        if code.startswith("day_"):
            _, per, day = code.split("_",2)
            return await show_day(msg, ctx, per, day, push=False)
        if code=="hist":
            return await show_history(msg, ctx, push=False)
        return await show_main(msg, ctx, push=False)

    # 기타
    if d.startswith("year_"):
        return await show_year(msg, ctx, d.split("_",1)[1])
    if d.startswith("mon_"):
        return await show_month(msg, ctx, d.split("_",1)[1])
    if d.startswith("tgl_"):
        _, per, fl = d.split("_",2)
        return await show_month(msg, ctx, per, fl)
    if d.startswith("day_"):
        _, per, day = d.split("_",2)
        return await show_day(msg, ctx, per, day)
    if d == "hist":
        return await show_history(msg, ctx)
    if d == "profit_now":
        s, e = bounds_today()
        return await show_profit(msg, ctx, s, e, "💰 Текущая ЗП")
    if d == "profit_prev":
        s, e = bounds_prev()
        return await show_profit(msg, ctx, s, e, "💼 Прошлая ЗП")
    if d == "kpi":
        return await show_kpi(msg, ctx, False)
    if d == "kpi_prev":
        return await show_kpi(msg, ctx, True)

# ─── COMMANDS & RUN ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data = {"entries": read_sheet(), "chats": set()}
    await update.message.reply_text("📊 <b>Главное меню</b>",
                                    parse_mode="HTML",
                                    reply_markup=main_kb())
    ctx.application.bot_data["chats"].add(update.effective_chat.id)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    # sync & reminder
    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logger.info("🚀 Bot up")
    app.run_polling(drop_pending_updates=True)