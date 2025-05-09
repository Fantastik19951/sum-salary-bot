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
if not os.path.exists("credentials.json"):
    env = os.getenv("GOOGLE_KEY_JSON")
    if env:
        with open("credentials.json", "w") as f:
            f.write(env)

TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
REMIND_HH_MM = (20, 0)
UNDO_WINDOW  = 10
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
    for idx,row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        e = {"date": d, "symbols": row[1].strip(), "row_idx": idx}
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        e["amount" if amt is not None else "salary"] = amt if amt is not None else sal
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
        try: dv = pdate(v.strip())
        except: continue
        if dv <= nd: ins = i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx, symbols, amount):
    if not SHEET: return
    SHEET.update_cell(idx, 2, symbols)
    SHEET.update_cell(idx, 3, amount)

def delete_row(idx):
    if SHEET: SHEET.delete_rows(idx)

# ─── SYNC & REMINDERS ────────────────────────────────────────────────────────
async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

async def reminder(ctx):
    for cid in ctx.application.bot_data.get("chats", set()):
        try: await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи сегодня!")
        except: pass

# ─── FORMAT ─────────────────────────────────────────────────────────────────
def fmt_amount(x: float) -> str:
    if x == int(x):
        return f"{int(x):,}".replace(",",".")
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    i,f = s.split(".")
    return f"{int(i):,}".replace(",",".") + "," + f

# ─── NAVIGATION ─────────────────────────────────────────────────────────────
def init_nav(ctx):
    ctx.user_data["nav"] = deque([("main","Главное меню")])

def push_nav(ctx, code, label):
    ctx.user_data.setdefault("nav", deque()).append((code,label))

def pop_nav(ctx):
    nav = ctx.user_data.get("nav", deque())
    if len(nav) > 1:
        nav.pop()
    return nav[-1] if nav else ("main","Главное меню")

# ─── UI HELPERS ─────────────────────────────────────────────────────────────
def main_kb():
    pad = "\u00A0"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{pad*4}📅 2024{pad*4}", "year_2024"),
         InlineKeyboardButton(f"{pad*4}📅 2025{pad*4}", "year_2025")],
        [InlineKeyboardButton(f"{pad*8}📆 Сегодня{pad*8}", "go_today")],
        [InlineKeyboardButton(f"{pad*8}➕ Запись{pad*8}", "add_rec")],
        [InlineKeyboardButton(f"{pad*6}💰 Текущая ЗП{pad*6}", "profit_now"),
         InlineKeyboardButton(f"{pad*6}💼 Прошлая ЗП{pad*6}", "profit_prev")],
        [InlineKeyboardButton(f"{pad*8}📜 История ЗП{pad*8}", "hist")],
        [InlineKeyboardButton(f"{pad*6}📊 KPI тек.{pad*6}", "kpi"),
         InlineKeyboardButton(f"{pad*6}📊 KPI прош.{pad*6}", "kpi_prev")],
    ])

def nav_buttons(ctx):
    code,label = pop_nav(ctx)
    return [
        InlineKeyboardButton(f"⬅️ {label}", "back"),
        InlineKeyboardButton("🏠 Главное", "main")
    ]

async def safe_edit(msg:Message, text:str, kb=None):
    kb = kb or InlineKeyboardMarkup([nav_buttons(msg._bot_data if False else ctx)])  # placeholder
    try:    return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except: return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ─── VIEW FUNCTIONS ─────────────────────────────────────────────────────────
async def show_main(msg:Message, ctx:ContextTypes.DEFAULT_TYPE):
    init_nav(ctx)
    ctx.application.bot_data["chats"].add(msg.chat_id)
    ctx.application.bot_data["entries"] = read_sheet()
    await safe_edit(msg, "📊 <b>Главное меню</b>", main_kb())

async def show_year(msg, ctx, year: str):
    push_nav(ctx, f"year_{year}", year)
    btns = [InlineKeyboardButton(MONTH_NAMES[i].capitalize(), f"mon_{year}-{i+1:02d}") for i in range(12)]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    rows.append(nav_buttons(ctx))
    await safe_edit(msg, f"📆 <b>{year}</b>", InlineKeyboardMarkup(rows))

async def show_month(msg, ctx, code: str, flag=None):
    year,mon = code.split("-")
    label = f"{MONTH_NAMES[int(mon)-1].capitalize()} {year}"
    push_nav(ctx, f"mon_{code}", label)
    D = ctx.application.bot_data["entries"].get(code, [])
    today = dt.date.today()
    if flag is None:
        flag = "old" if today.strftime("%Y-%m")==code and today.day<=15 else "new"
    part = [e for e in D if (pdate(e["date"]).day<=15)==(flag=="old")]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    header = f"<b>{label} · {'01–15' if flag=='old' else '16–31'}</b>"
    body   = "\n".join(f"{d} · {sum(e['amount'] for e in part if e['date']==d):.2f} $" for d in days) or "Нет записей"
    footer = f"<b>Итого: {total:.2f} $</b>"
    togg = "new" if flag=="old" else "old"
    rows = [[InlineKeyboardButton("Первая"+(" половина" if flag=="old" else " вторая"), f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, f"day_{code}_{d}")])
    rows.append(nav_buttons(ctx))
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_day(msg, ctx, code: str, date: str):
    push_nav(ctx, f"day_{code}_{date}", date)
    ctx.application.bot_data["entries"] = read_sheet()
    ents = [e for e in ctx.application.bot_data["entries"].get(code, []) if e["date"]==date]
    total = sum(e["amount"] for e in ents)
    header = f"<b>{date}</b>"
    body   = "\n".join(f"{i+1}. {e['symbols']} · {e['amount']:.2f} $" for i,e in enumerate(ents)) or "Нет записей"
    footer = f"<b>Итого: {total:.2f} $</b>"
    rows = [
        [InlineKeyboardButton(f"❌{i+1}", f"drow_{e['row_idx']}_{code}_{date}"),
         InlineKeyboardButton(f"✏️{i+1}", f"edit_{e['row_idx']}_{code}_{date}")]
        for i,e in enumerate(ents)
    ]
    rows.append([InlineKeyboardButton("➕ Добавить", f"add_{code}_{date}")])
    rows.append(nav_buttons(ctx))
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_history(msg, ctx):
    push_nav(ctx, "hist", "История")
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        text = "История пуста"
    else:
        lines = [f"• {pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year} — {e['salary']:.2f} $" for e in sorted(ents, key=lambda x:pdate(x["date"]))]
        text = "<b>📜 История ЗП</b>\n" + "\n".join(lines)
    await safe_edit(msg, text, InlineKeyboardMarkup([nav_buttons(ctx)]))

async def show_profit(msg, ctx, start, end, title: str):
    push_nav(ctx, title, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if start<=pdate(e["date"])<=end]
    tot = sum(e["amount"] for e in ents)
    text = f"{title} ({sdate(start)}–{sdate(end)})\n<b>10%: {tot*0.1:.2f} $</b>"
    await safe_edit(msg, text, InlineKeyboardMarkup([nav_buttons(ctx)]))

async def show_kpi(msg, ctx, prev: bool):
    if prev:
        s,e = bounds_prev(); title="📊 KPI прошлое"
    else:
        s,e = bounds_today(); title="📊 KPI текущее"
    push_nav(ctx, title, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if s<=pdate(e["date"])<=e]
    if not ents:
        return await safe_edit(msg, "Нет данных", InlineKeyboardMarkup([nav_buttons(ctx)]))
    turn = sum(e["amount"] for e in ents)
    sal  = turn*0.1
    days = len({e["date"] for e in ents})
    text = (
        f"{title} ({sdate(s)}–{sdate(e)})\n"
        f"Оборот: {turn:.2f} $\n"
        f"ЗП10%: {sal:.2f} $\n"
        f"Дней: {days}"
    )
    await safe_edit(msg, text, InlineKeyboardMarkup([nav_buttons(ctx)]))

# ─── ADD/EDIT FLOW ─────────────────────────────────────────────────────────
async def ask_date(msg, ctx):
    prompt = await msg.reply_text("📅 Введите дату или «Сегодня»")
    ctx.user_data["flow"] = {"step":"date","msg":msg,"prompt":prompt}

async def ask_name(msg, ctx):
    flow = ctx.user_data["flow"]
    text = "✏️ Введите имя"
    if flow.get("mode")=="edit":
        text += f" (старое: {flow['old_symbols']})"
    prompt = await msg.reply_text(text)
    flow.update({"step":"sym","prompt":prompt})

async def ask_amount(msg, ctx):
    flow = ctx.user_data["flow"]
    text = "💰 Введите сумму"
    if flow.get("mode")=="edit":
        text += f" (старое: {flow['old_amount']:.2f})"
    prompt = await msg.reply_text(text)
    flow.update({"step":"amt","prompt":prompt})

async def process_text(u:Update, ctx:ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow: return
    txt = u.message.text.strip()
    await u.message.delete(); await flow["prompt"].delete()
    if flow["step"]=="date":
        if txt.lower()=="сегодня":
            flow["date"] = sdate(dt.date.today())
        elif is_date(txt):
            flow["date"] = txt
        else:
            return await flow["msg"].reply_text("Неверный формат")
        return await ask_name(flow["msg"], ctx)
    if flow["step"]=="sym":
        flow["symbols"] = txt
        if flow.get("mode")=="edit":
            idx=flow["row"]
            old = next(e for e in ctx.application.bot_data["entries"][flow["date"][:7]] if e["row_idx"]==idx)
            flow["old_amount"] = old["amount"]
        return await ask_amount(flow["msg"], ctx)
    if flow["step"]=="amt":
        try: v=float(txt.replace(",","."))
        except: return await flow["msg"].reply_text("Нужно число")
        date = flow["date"]; code=date[:7]
        # EDIT
        if flow.get("mode")=="edit":
            idx=flow["row"]
            update_row(idx, flow["symbols"], v)
            ctx.application.bot_data["entries"] = read_sheet()
            note=await flow["msg"].reply_text("✅ Обновлено", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↺ Отменить", f"undo_edit_{idx}")]]
            ))
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(note.chat.id, note.message_id),
                when=UNDO_WINDOW
            )
            ctx.user_data.pop("flow")
            return await show_day(flow["msg"], ctx, code, date)
        # ADD
        flow["amount"] = v
        idx = push_row(flow)
        ctx.application.bot_data["entries"] = read_sheet()
        note=await flow["msg"].reply_text(f"✅ Добавлено: {flow['symbols']} – {v:.2f} $", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↺ Отменить", f"undo_{idx}")]]
        ))
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(note.chat.id, note.message_id),
            when=UNDO_WINDOW
        )
        ctx.user_data.pop("flow")
        return await show_day(flow["msg"], ctx, code, date)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=upd.callback_query
    if not q: return
    data, msg = q.data, q.message
    await q.answer()
    if data=="back":
        code,label = pop_nav(ctx)
        data=code
    if data=="main":
        return await show_main(msg, ctx)
    if data=="year_2024" or data=="year_2025":
        return await show_year(msg, ctx, data.split("_",1)[1])
    if data.startswith("mon_"):
        return await show_month(msg, ctx, data.split("_",1)[1])
    if data.startswith("tgl_"):
        _,code,fl = data.split("_",2)
        return await show_month(msg, ctx, code, fl)
    if data.startswith("day_"):
        _,code,day = data.split("_",2)
        return await show_day(msg, ctx, code, day)
    if data=="go_today":
        d=sdate(dt.date.today()); return await show_day(msg, ctx, d[:7], d)
    if data=="add_rec":
        return await ask_date(msg, ctx)
    if data.startswith("add_"):
        _,code,date = data.split("_",2)
        ctx.user_data["flow"]={"step":"sym","mode":"add","date":date,"msg":msg}
        return await ask_name(msg, ctx)
    if data.startswith("drow_"):
        _,r,code,day = data.split("_",4)[:4]
        delete_row(int(r)); ctx.application.bot_data["entries"]=read_sheet()
        await msg.reply_text("🚫 Удалено")
        return await show_day(msg, ctx, code, day)
    if data.startswith("edit_"):
        _,row,code,day = data.split("_",4)[:4]; row=int(row)
        old=next(e for e in ctx.application.bot_data["entries"][code] if e["row_idx"]==row)
        ctx.user_data["flow"]={"step":"sym","mode":"edit","row":row,
                               "date":day,"old_symbols":old["symbols"],"msg":msg}
        return await ask_name(msg, ctx)
    if data.startswith("undo_"):
        _,idx = data.split("_",1); idx=int(idx)
        delete_row(idx); ctx.application.bot_data["entries"]=read_sheet()
        return await msg.reply_text("↺ Добавление отменено")
    if data.startswith("undo_edit_"):
        return await msg.reply_text("↺ Отмена редактирования")  # for simplicity
    if data=="hist":
        return await show_history(msg, ctx)
    if data=="profit_now":
        s,e=bounds_today(); return await show_profit(msg, ctx, s, e, "💰 Текущая ЗП")
    if data=="profit_prev":
        s,e=bounds_prev(); return await show_profit(msg, ctx, s, e, "💼 Прошлая ЗП")
    if data=="kpi":
        return await show_kpi(msg, ctx, False)
    if data=="kpi_prev":
        return await show_kpi(msg, ctx, True)

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data = {"entries": read_sheet(), "chats": set()}
    await show_main(update.message, ctx)

if __name__=="__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh,mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))
    logging.info("🚀 Bot up")
    app.run_polling()