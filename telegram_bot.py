import os
import logging
import datetime as dt
import re
from collections import defaultdict, deque
from io import BytesIO

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
if not os.path.exists("credentials.json"):
    env = os.getenv("GOOGLE_KEY_JSON")
    if env:
        with open("credentials.json", "w") as f:
            f.write(env)

TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10   # seconds
REMIND_HH_MM = (20, 0)
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
except Exception as e:
    logging.error(f"Sheets connection failed: {e}")
    SHEET = None

def safe_float(s):
    try: return float(s.replace(",", "."))
    except: return None

def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s, DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))

def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx, row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None: continue
        if sal is not None: e["salary"] = sal
        else:             e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row = [
        entry["date"],
        entry.get("symbols", ""),
        entry.get("amount", ""),
        entry.get("salary", ""),
    ]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try: dv = pdate(v.strip())
        except: continue
        if dv <= nd: ins = i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def delete_row(idx):
    if SHEET: SHEET.delete_rows(idx)

def update_row(idx, symbols, amount):
    if not SHEET: return
    SHEET.update_cell(idx, 2, symbols)
    SHEET.update_cell(idx, 3, amount)

# ─── SYNC & REMINDER ────────────────────────────────────────────────────────
async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

async def reminder(ctx):
    for cid in ctx.application.bot_data.get("chats", set()):
        try: await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи сегодня!")
        except: pass

# ─── NAV STACK ──────────────────────────────────────────────────────────────
def push_nav(ctx, code, label):
    stack = ctx.user_data.setdefault("nav", deque())
    stack.append({"code":code, "label":label})

def pop_nav(ctx):
    stack = ctx.user_data.get("nav", deque())
    if len(stack) > 1:
        stack.pop()  # remove current
        prev = stack[-1]
        return prev["code"], prev["label"]
    return "main", "Главное меню"

def peek_nav(ctx):
    stack = ctx.user_data.get("nav", deque())
    if len(stack) > 1:
        prev = stack[-2]
        return prev["code"], prev["label"]
    return "main", "Главное меню"

# ─── UI HELPERS ─────────────────────────────────────────────────────────────
def nav_row(ctx):
    code,label = peek_nav(ctx)
    return [
        InlineKeyboardButton(f"⬅️ {label}", callback_data=code),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]

async def safe_edit(msg:Message, text:str, kb:InlineKeyboardMarkup):
    try: return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except: return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def bounds_today():
    d=dt.date.today()
    start = d.replace(day=1) if d.day<=15 else d.replace(day=16)
    return start, d

def bounds_prev():
    d=dt.date.today()
    if d.day <= 15:
        last = d.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=16), last
    return d.replace(day=1), d.replace(day=15)

# ─── FORMAT AMOUNT ──────────────────────────────────────────────────────────
def fmt_amount(x:float) -> str:
    if x == int(x):
        return f"{int(x):,}".replace(",", ".")
    s = f"{x:.2f}"
    i,f = s.split(".")
    f = f.rstrip("0")
    return f"{int(i):,}".replace(",", ".") + "," + f

# ─── MAIN MENU ──────────────────────────────────────────────────────────────
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

async def show_main(msg, ctx):
    ctx.user_data["nav"] = deque([{"code":"main","label":"Главное меню"}])
    await safe_edit(msg, "<b>📊 Главное меню</b>", main_kb())

# ─── YEAR VIEW ──────────────────────────────────────────────────────────────
async def show_year(msg, ctx, year):
    code  = f"year_{year}"
    label = year
    push_nav(ctx, code, label)
    btns = [InlineKeyboardButton(MONTH_NAMES[i].capitalize(),
               callback_data=f"mon_{year}-{i+1:02d}") for i in range(12)]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    rows.append(nav_row(ctx))
    await safe_edit(msg, f"<b>📆 {year}</b>", InlineKeyboardMarkup(rows))

# ─── MONTH VIEW ─────────────────────────────────────────────────────────────
async def show_month(msg, ctx, code, flag=None, push=True):
    year,mon = code.split("-")
    mname = MONTH_NAMES[int(mon)-1].capitalize()
    cb    = f"mon_{code}"
    label = f"{mname} {year}"
    if push:
        push_nav(ctx, cb, label)

    today = dt.date.today()
    if flag is None:
        flag = "old" if today.strftime("%Y-%m")==code and today.day<=15 else "new"

    ents = ctx.application.bot_data["entries"].get(code, [])
    part = [e for e in ents if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)

    header = f"<b>{mname} {year} · {'01–15' if flag=='old' else '16–31'}</b>"
    body = "\n".join(f"{e['date']} · {e['symbols']} · {fmt_amount(e['amount'])} $" for e in part) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"

    togg = "new" if flag=="old" else "old"
    rows = [[InlineKeyboardButton(
        "Первая половина" if flag=="old" else "Вторая половина",
        callback_data=f"tgl_{code}_{togg}"
    )]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append(nav_row(ctx))
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

# ─── DAY VIEW ────────────────────────────────────────────────────────────────
async def show_day(msg, ctx, code, date, push=True):
    cb    = f"day_{code}_{date}"
    label = date
    if push:
        push_nav(ctx, cb, label)
    ctx.user_data["last_day"] = (msg, code, date)

    ents = [e for e in ctx.application.bot_data["entries"].get(code, []) if e["date"]==date and "amount" in e]
    total = sum(e["amount"] for e in ents)

    header = f"<b>{date}</b>"
    body = "\n".join(f"{i}. {e['symbols']} · {fmt_amount(e['amount'])} $" for i,e in enumerate(ents,1)) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"

    rows = [
        [
            InlineKeyboardButton(f"❌{i}", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ]
        for i,e in enumerate(ents,1)
    ]
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data=f"add_{code}_{date}")])
    rows.append(nav_row(ctx))

    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

# ─── HISTORY VIEW ───────────────────────────────────────────────────────────
async def show_history(msg, ctx):
    code  = "hist"
    label = "История ЗП"
    push_nav(ctx, code, label)

    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        text = "История пуста"
    else:
        lines = []
        for e in sorted(ents, key=lambda x:pdate(x["date"])):
            d   = pdate(e["date"])
            sal = e["salary"]
            lines.append(f"• {d.day} {MONTH_NAMES[d.month-1]} {d.year} — {fmt_amount(sal)} $")
        text = "<b>📜 История ЗП</b>\n" + "\n".join(lines)

    await safe_edit(msg, text, InlineKeyboardMarkup([nav_row(ctx)]))

# ─── PROFIT & KPI VIEWS ────────────────────────────────────────────────────
async def show_profit(msg, ctx, start, end, title, code_lab):
    push_nav(ctx, code_lab, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start<=pdate(e["date"])<=end and "amount" in e]
    tot = sum(e["amount"] for e in ents)
    text = f"{title} ({sdate(start)} – {sdate(end)})\n<b>10%: {fmt_amount(tot*0.10)} $</b>"
    await safe_edit(msg, text, InlineKeyboardMarkup([nav_row(ctx)]))

async def show_kpi(msg, ctx, prev=False):
    if prev:
        start,end = bounds_prev()
        title     = "📊 KPI прошлого периода"
        code_lab  = "kpi_prev"
    else:
        start,end = bounds_today()
        title     = "📊 KPI текущего периода"
        code_lab  = "kpi"
    push_nav(ctx, code_lab, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start<=pdate(e["date"])<=end and "amount" in e]
    if not ents:
        return await safe_edit(msg, "Нет данных", InlineKeyboardMarkup([nav_row(ctx)]))
    turn = sum(e["amount"] for e in ents)
    sal  = turn * 0.10
    days = len({e["date"] for e in ents})
    plen = (end - start).days + 1
    avg  = sal / days if days else 0
    text = (
        f"{title} ({sdate(start)} – {sdate(end)})\n"
        f"• Оборот: {fmt_amount(turn)} $\n"
        f"• ЗП 10%: {fmt_amount(sal)} $\n"
        f"• Дней: {days}/{plen}\n"
        f"• Ср/день: {fmt_amount(avg)} $"
    )
    await safe_edit(msg, text, InlineKeyboardMarkup([nav_row(ctx)]))

# ─── ADD / EDIT FLOW ─────────────────────────────────────────────────────────
async def ask_rec(msg, ctx):
    prompt = await msg.reply_text(
        "📅 Введите дату (ДД.MM.YYYY) или нажмите «Сегодня»",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today_add")]])
    )
    ctx.user_data["flow"] = {"step":"date", "prompt":prompt}

async def ask_fixed(msg, ctx, code, date):
    flow = {"step":"sym", "mode":"add", "date":date, "prompt":None}
    ctx.user_data["flow"] = flow
    prompt = await msg.reply_text("✏️ Введите имя:")
    flow["prompt"] = prompt

async def ask_sal(msg, ctx):
    prompt = await msg.reply_text("💰 Введите сумму:")
    ctx.user_data["flow"] = {"step":"val", "mode":"salary", "date":sdate(dt.date.today()), "prompt":prompt}

async def process_text(u:Update, ctx:ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow:
        return
    txt = u.message.text.strip()
    try: await u.message.delete()
    except: pass
    try: await flow["prompt"].delete()
    except: pass

    # DATE
    if flow["step"] == "date":
        if txt.lower() in ("сегодня","today"):
            flow["date"] = sdate(dt.date.today())
        elif is_date(txt):
            flow["date"] = txt
        else:
            return await u.message.reply_text("Неверный формат даты")
        flow["step"] = "sym"
        prompt = await u.message.reply_text("✏️ Введите имя:")
        flow["prompt"] = prompt
        return

    # NAME
    if flow["step"] == "sym":
        flow["symbols"] = txt
        flow["step"] = "val"
        if flow.get("mode") == "edit":
            idx = flow["row"]
            old = next(
                e for e in ctx.application.bot_data["entries"]
                .get(flow["date"][:7],[]) if e["row_idx"] == idx
            )
            flow["old_symbols"] = old["symbols"]
            flow["old_amount"]  = old.get("amount") or old.get("salary") or 0
        prompt = await u.message.reply_text("💰 Введите сумму:")
        flow["prompt"] = prompt
        return

    # VALUE
    if flow["step"] == "val":
        try: val = float(txt.replace(",","."))
        except:
            return await u.message.reply_text("Нужно число")
        # EDIT
        if flow.get("mode") == "edit":
            idx = flow["row"]
            update_row(idx, flow["symbols"], val)
            ctx.application.bot_data["entries"] = read_sheet()
            ctx.user_data.pop("flow", None)
            resp = await u.message.reply_text(
                "✏️ Запись обновлена",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить", callback_data=f"undo_edit_{idx}")
                ]])
            )
            ctx.user_data["undo_edit"] = {
                "row": idx,
                "old_symbols": flow["old_symbols"],
                "old_amount": flow["old_amount"],
                "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
            }
            # refresh day view without pushing nav
            last_msg, code, date = ctx.user_data.get("last_day", (resp, flow["date"][:7], flow["date"]))
            return await show_day(last_msg, ctx, code, date, push=False)

        # ADD
        flow["amount"] = val
        row = push_row(flow)
        ctx.application.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("flow", None)
        resp = await u.message.reply_html(
            f"✅ Добавлено: <b>{flow['symbols']}</b> — <b>{fmt_amount(val)} $</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
            ]])
        )
        ctx.user_data["undo"] = {
            "row": row,
            "expires": dt.datetime.utcnow() + dt.timedelta(seconds=UNDO_WINDOW)
        }
        # auto-delete after 10s
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id, resp.message_id),
            when=UNDO_WINDOW
        )
        # refresh day view without nav push
        last_msg, code, date = ctx.user_data.get("last_day", (resp, flow["date"][:7], flow["date"]))
        return await show_day(last_msg, ctx, code, date, push=False)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q: return
    data, msg = q.data, q.message
    await q.answer()

    # BACK button
    if data == "back":
        data, _ = pop_nav(ctx)

    # MAIN
    if data == "main":
        return await show_main(msg, ctx)

    # ADD flow triggers
    if data == "today_add":
        flow = ctx.user_data.get("flow", {})
        flow["date"] = sdate(dt.date.today())
        flow["step"] = "sym"
        try: await msg.delete()
        except: pass
        prompt = await msg.reply_text("✏️ Введите имя:")
        flow["prompt"] = prompt
        return
    if data == "add_rec":
        return await ask_rec(msg, ctx)
    if data.startswith("add_") and data != "add_rec":
        _, code, date = data.split("_", 2)
        return await ask_fixed(msg, ctx, code, date)
    if data == "add_sal":
        return await ask_sal(msg, ctx)

    # YEAR
    if data.startswith("year_"):
        return await show_year(msg, ctx, data.split("_",1)[1])

    # MONTH
    if data.startswith("mon_"):
        return await show_month(msg, ctx, data.split("_",1)[1], push=True)
    if data.startswith("tgl_"):
        _, code, fl = data.split("_",2)
        return await show_month(msg, ctx, code, flag=fl, push=False)

    # DAY
    if data.startswith("day_"):
        _, code, day = data.split("_",2)
        return await show_day(msg, ctx, code, day, push=True)
    if data == "go_today":
        code = dt.date.today().strftime("%Y-%m")
        day  = sdate(dt.date.today())
        return await show_day(msg, ctx, code, day, push=True)

    # DELETE
    if data.startswith("drow_"):
        _, row, code, day = data.split("_",4)[:4]
        delete_row(int(row))
        ctx.application.bot_data["entries"] = read_sheet()
        r = await msg.reply_text("🚫 Удалено")
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(r.chat.id, r.message_id),
            when=UNDO_WINDOW
        )
        return await show_day(msg, ctx, code, day, push=False)

    # EDIT
    if data.startswith("edit_"):
        _, row, code, day = data.split("_",4)[:4]
        row = int(row)
        e = next(e for e in ctx.application.bot_data["entries"].get(code, []) if e["row_idx"] == row)
        ctx.user_data["flow"] = {"step":"sym", "mode":"edit", "row":row, "date":day, "prompt":None}
        prompt = await msg.reply_text(f"✏️ Новое имя (было {e['symbols']}):")
        ctx.user_data["flow"]["prompt"] = prompt
        return

    # UNDO ADD
    if data.startswith("undo_"):
        row = int(data.split("_",1)[1])
        udata = ctx.user_data.get("undo", {})
        if udata.get("row") == row and dt.datetime.utcnow() <= udata.get("expires", dt.datetime.min):
            delete_row(row)
            ctx.application.bot_data["entries"] = read_sheet()
            try: await msg.delete()
            except: pass
            r = await msg.reply_text("↺ Добавление отменено")
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(r.chat.id, r.message_id),
                when=UNDO_WINDOW
            )
        else:
            await msg.reply_text("⏱ Время вышло")
        return

    # UNDO EDIT
    if data.startswith("undo_edit_"):
        row = int(data.split("_",1)[1])
        udata = ctx.user_data.get("undo_edit", {})
        if udata.get("row") == row and dt.datetime.utcnow() <= udata.get("expires", dt.datetime.min):
            update_row(row, udata["old_symbols"], udata["old_amount"])
            ctx.application.bot_data["entries"] = read_sheet()
            try: await msg.delete()
            except: pass
            r = await msg.reply_text("↺ Изменение отменено")
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(r.chat.id, r.message_id),
                when=UNDO_WINDOW
            )
        else:
            await msg.reply_text("⏱ Время вышло")
        return

    # PROFIT / HISTORY / KPI
    if data == "profit_now":
        s,e = bounds_today()
        return await show_profit(msg, ctx, s, e, "💰 Текущая ЗП", "profit_now")
    if data == "profit_prev":
        s,e = bounds_prev()
        return await show_profit(msg, ctx, s, e, "💼 Прошлая ЗП", "profit_prev")
    if data == "hist":
        return await show_history(msg, ctx)
    if data == "kpi":
        return await show_kpi(msg, ctx, False)
    if data == "kpi_prev":
        return await show_kpi(msg, ctx, True)

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data.setdefault("chats", set()).add(update.effective_chat.id)
    ctx.application.bot_data["entries"] = read_sheet()
    await show_main(update.message, ctx)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data = {}
    app.bot_data["entries"] = read_sheet()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()