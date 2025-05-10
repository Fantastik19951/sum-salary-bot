# telegram_bot.py

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
UNDO_WINDOW  = 10      # seconds for undo
REMIND_HH_MM = (20, 0) # daily reminder at 20:00
MONTH_NAMES  = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря"
]
# отступ для всех кнопок
PAD = "\u00A0"*2

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
    if not SHEET: return data
    for idx,row in enumerate(SHEET.get_all_values(), start=1):
        if idx <= HEADER_ROWS or len(row)<2: continue
        d=row[0].strip()
        if not is_date(d): continue
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        e={"date":d,"symbols":row[1].strip(),"row_idx":idx}
        if sal is not None: e["salary"]=sal
        else: e["amount"]=amt
        key=f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row = [entry["date"], entry.get("symbols",""), entry.get("amount",""), entry.get("salary","")]
    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i,v in enumerate(col, start=HEADER_ROWS+1):
        try:
            if pdate(v)<=nd: ins=i
            else: break
        except: continue
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx:int, symbols:str, amount:float):
    if not SHEET: return
    SHEET.update_cell(idx,2,symbols)
    SHEET.update_cell(idx,3,amount)

def delete_row(idx:int):
    if SHEET: SHEET.delete_rows(idx)

# ─── SYNC & REMINDER ────────────────────────────────────────────────────────
async def auto_sync(ctx):
    ctx.application.bot_data["entries"] = read_sheet()

async def reminder(ctx):
    for cid in ctx.application.bot_data.get("chats", set()):
        try: await ctx.bot.send_message(cid,"⏰ Не забудьте внести записи сегодня!")
        except: pass

# ─── NAV STACK ──────────────────────────────────────────────────────────────
def init_nav(ctx):
    ctx.user_data["nav"] = deque([("main","Главная")])

def push_nav(ctx,code,label):
    ctx.user_data.setdefault("nav",deque()).append((code,label))

def pop_view(ctx):
    nav = ctx.user_data.get("nav",deque())
    if len(nav)>1: nav.pop()
    return nav[-1]

def peek_prev(ctx):
    nav = ctx.user_data.get("nav",deque())
    return nav[-2] if len(nav)>=2 else nav[-1]

def nav_kb(ctx, hide_back=False):
    buttons = []
    if not hide_back:
        prev_code, prev_label = peek_prev(ctx)
        buttons.append(InlineKeyboardButton(f"⬅️ {prev_label}", callback_data="back"))
    buttons.append(InlineKeyboardButton("🏠 Главное", callback_data="main"))
    return InlineKeyboardMarkup([buttons])

# ─── UI & FORMAT ────────────────────────────────────────────────────────────
def fmt_amount(x:float)->str:
    if abs(x-int(x))<1e-9:
        return f"{int(x):,}".replace(",",".")
    s=f"{x:.2f}".rstrip("0").rstrip(".")
    i,f=s.split(".") if "." in s else (s,"")
    return f"{int(i):,}".replace(",",".") + (f and ","+f)

def bounds_today():
    d=dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16)), d

def bounds_prev():
    d=dt.date.today()
    if d.day<=15:
        last = d.replace(day=1) - dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

# клавиатура главного меню
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{PAD}📅 2024{PAD}", "year_2024"),
         InlineKeyboardButton(f"{PAD}📅 2025{PAD}", "year_2025")],
        [InlineKeyboardButton(f"{PAD}📆 Сегодня{PAD}", "go_today")],
        [InlineKeyboardButton(f"{PAD}➕ Запись{PAD}", "add_rec")],
        [InlineKeyboardButton(f"{PAD}💵 Зарплата{PAD}", "add_sal")],
        [InlineKeyboardButton(f"{PAD*5}💰 Текущая ЗП{PAD*10}", "profit_now"),
         InlineKeyboardButton(f"{PAD*5}💼 Прошлая ЗП{PAD*10}", "profit_prev")],
        [InlineKeyboardButton(f"{PAD}📜 История ЗП{PAD}", "hist")],
        [InlineKeyboardButton(f"{PAD}📊 KPI тек.{PAD}", "kpi"),
         InlineKeyboardButton(f"{PAD}📊 KPI прош.{PAD}", "kpi_prev")],
    ])

# ─── VIEW FUNCTIONS ─────────────────────────────────────────────────────────
async def show_main(msg:Message, ctx:ContextTypes.DEFAULT_TYPE, push=True):
    if push: init_nav(ctx)
    ctx.application.bot_data.setdefault("chats", set()).add(msg.chat_id)
    ctx.application.bot_data["entries"] = read_sheet()
    text = "<b>📊 Главное меню</b>"
    return await msg.edit_text(text, parse_mode="HTML", reply_markup=main_kb())

async def show_year(msg,ctx,year,push=True):
    if push: push_nav(ctx, f"year_{year}", year)
    btns = [InlineKeyboardButton(m.capitalize(), callback_data=f"mon_{year}-{i+1:02d}")
            for i,m in enumerate(MONTH_NAMES)]
    rows = [btns[i:i+4] for i in range(0,12,4)]
    return await safe_edit(msg, f"<b>📆 {year}</b>", InlineKeyboardMarkup(rows + nav_kb(ctx).inline_keyboard))

async def show_month(msg,ctx,code,flag=None,push=True):
    year,mon = code.split("-")
    label = f"{MONTH_NAMES[int(mon)-1].capitalize()} {year}"
    if push: push_nav(ctx, f"mon_{code}", label)
    today = dt.date.today()
    if flag is None:
        flag = "old" if today.strftime("%Y-%m")==code and today.day<=15 else "new"
    ents = ctx.application.bot_data["entries"].get(code, [])
    part = [e for e in ents if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    header = f"<b>{label} · {'01–15' if flag=='old' else '16–31'}</b>"
    body = "\n".join(f"{d} · {fmt_amount(sum(e['amount'] for e in part if e['date']==d))} $"
                     for d in days) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"
    togg = "new" if flag=="old" else "old"
    rows = [[InlineKeyboardButton("Первая половина" if flag=="old" else "Вторая половина",
                                  callback_data=f"tgl_{code}_{togg}")]]
    rows += [[InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")] for d in days]
    return await safe_edit(msg, "\n".join([header,body,"",footer]),
                           InlineKeyboardMarkup(rows + nav_kb(ctx).inline_keyboard))

async def show_day(msg,ctx,code,date,push=True):
    if push: push_nav(ctx, f"day_{code}_{date}", date)
    ctx.application.bot_data["entries"] = read_sheet()
    ents = [e for e in ctx.application.bot_data["entries"].get(code,[]) if e["date"]==date and "amount" in e]
    total = sum(e["amount"] for e in ents)
    header = f"<b>{date}</b>"
    body = "\n".join(f"{i+1}. {e['symbols']} · {fmt_amount(e['amount'])} $"
                     for i,e in enumerate(ents)) or "Нет записей"
    footer = f"<b>Итого: {fmt_amount(total)} $</b>"
    rows = []
    for i,e in enumerate(ents):
        rows.append([
            InlineKeyboardButton(f"❌{i+1}", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i+1}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ])
    rows.append([InlineKeyboardButton("➕ Запись", callback_data=f"add_{code}_{date}")])
    return await safe_edit(msg, "\n".join([header,body,"",footer]),
                           InlineKeyboardMarkup(rows + nav_kb(ctx).inline_keyboard))

async def show_history(msg,ctx,push=True):
    if push: push_nav(ctx, "hist", "История ЗП")
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        text = "История пуста"
    else:
        lines = [f"• {pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year}"
                 f" — {fmt_amount(e['salary'])} $"
                 for e in sorted(ents, key=lambda x: pdate(x['date']))]
        text = "<b>📜 История ЗП</b>\n" + "\n".join(lines)
    return await safe_edit(msg, text, nav_kb(ctx, hide_back=True))

async def show_profit(msg,ctx,start,end,title,push=True):
    if push: push_nav(ctx, title, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start<=pdate(e['date'])<=end and "amount" in e]
    tot = sum(e["amount"] for e in ents)
    text = f"{title} ({sdate(start)} – {sdate(end)})\n<b>10%: {fmt_amount(tot*0.10)} $</b>"
    return await safe_edit(msg, text, nav_kb(ctx, hide_back=True))

async def show_kpi(msg,ctx,prev=False,push=True):
    if prev:
        start,end=bounds_prev(); title="📊 KPI прошлого"
    else:
        start,end=bounds_today(); title="📊 KPI текущего"
    if push: push_nav(ctx, title, title)
    ents = [e for v in ctx.application.bot_data["entries"].values() for e in v
            if start<=pdate(e['date'])<=end and "amount" in e]
    if not ents:
        return await safe_edit(msg, "Нет данных", nav_kb(ctx, hide_back=True))
    tot = sum(e["amount"] for e in ents)
    days_filled = len({e["date"] for e in ents})
    avg_per_day = (tot*0.10)/days_filled if days_filled else 0
    text = (
        f"{title} ({sdate(start)} – {sdate(end)})\n"
        f"• Оборот: {fmt_amount(tot)} $\n"
        f"• ЗП10%: {fmt_amount(tot*0.10)} $\n"
        f"• Заполнено дней: {days_filled}/15\n"
        f"• Ср/день: {fmt_amount(avg_per_day)} $"
    )
    return await safe_edit(msg, text, nav_kb(ctx, hide_back=True))

# ─── ADD/EDIT FLOW ──────────────────────────────────────────────────────────
async def ask_date(msg,ctx):
    prompt = await msg.reply_text(
        "📅 Введите дату (ДД.MM.YYYY) или «Сегодня»",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today_add")]])
    )
    ctx.user_data["flow"] = {"step":"date","msg":msg,"prompt":prompt}

async def ask_name(msg,ctx):
    flow = ctx.user_data["flow"]
    if flow.get("mode")=="edit":
        prompt = await msg.reply_text(f"✏️ Введите имя (старое: {flow['old_symbols']}):")
    else:
        prompt = await msg.reply_text("✏️ Введите имя:")
    flow.update({"step":"sym","prompt":prompt})

async def ask_amount(msg,ctx):
    flow = ctx.user_data["flow"]
    if flow.get("mode")=="edit":
        prev = flow["old_amount"]
        prompt = await msg.reply_text(f"💰 Введите сумму (старое: {fmt_amount(prev)} $):")
    else:
        prompt = await msg.reply_text("💰 Введите сумму:")
    flow.update({"step":"val","prompt":prompt})

async def process_text(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow: return
    txt = u.message.text.strip()
    await u.message.delete()
    try: await flow["prompt"].delete()
    except: pass

    # DATE STEP
    if flow["step"]=="date":
        if txt.lower()=="сегодня":
            flow["date"] = sdate(dt.date.today())
        elif is_date(txt):
            flow["date"] = txt
        else:
            return await flow["msg"].reply_text("Неверный формат даты")
        return await ask_name(flow["msg"],ctx)

    # NAME STEP
    if flow["step"]=="sym":
        flow["symbols"] = txt
        return await ask_amount(flow["msg"],ctx)

    # AMOUNT STEP
    if flow["step"]=="val":
        try:
            val = float(txt.replace(",","."))
        except:
            return await flow["msg"].reply_text("Нужно число")
        date_str = flow["date"]
        code = date_str[:7].replace(".","-")

        # EDIT MODE
        if flow.get("mode")=="edit":
            idx = flow["row"]
            update_row(idx, flow["symbols"], val)
            ctx.application.bot_data["entries"] = read_sheet()
            notify = await flow["msg"].reply_text(
                "✅ Данные изменены",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить", callback_data=f"undo_edit_{idx}")
                ]])
            )
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(notify.chat.id, notify.message_id),
                when=UNDO_WINDOW
            )
            ctx.user_data["undo_edit"] = {
                "row":idx,
                "old_symbols":flow["old_symbols"],
                "old_amount":flow["old_amount"],
                "code":code,
                "date":date_str,
                "expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)
            }
            ctx.user_data.pop("flow")
            return await show_day(flow["msg"],ctx,code,date_str)

        # ADD MODE
        flow["amount"] = val
        row = push_row(flow)
        ctx.application.bot_data["entries"] = read_sheet()
        notify = await flow["msg"].reply_text(
            f"✅ Добавлено: {flow['symbols']} · {fmt_amount(val)} $",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")
            ]])
        )
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(notify.chat.id, notify.message_id),
            when=UNDO_WINDOW
        )
        ctx.user_data["undo"] = {
            "row":row,
            "code":code,
            "date":date_str,
            "expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)
        }
        ctx.user_data.pop("flow")
        return await show_day(flow["msg"],ctx,code,date_str)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q: return
    await q.answer()
    d,msg = q.data, q.message

    # MAIN NAV
    if d=="main": return await show_main(msg,ctx,push=False)
    if d=="back":
        code,label = pop_view(ctx)
        # подключаемся к нужному view без пуша
        if code=="main": return await show_main(msg,ctx,push=False)
        if code.startswith("year_"):
            y=code.split("_",1)[1]
            return await show_year(msg,ctx,y,push=False)
        if code.startswith("mon_"):
            c=code.split("_",1)[1]
            return await show_month(msg,ctx,c,None,push=False)
        if code.startswith("day_"):
            _,c,dd = code.split("_",2)
            return await show_day(msg,ctx,c,dd,push=False)

    # START
    if d=="add_rec":      return await ask_date(msg,ctx)
    if d=="today_add":    # from date prompt
        ctx.user_data["flow"] = {"step":"date","msg":msg}
        return await process_text(upd,ctx)

    if d.startswith("add_"):
        _,code,day = d.split("_",2)
        ctx.user_data["flow"] = {"step":"sym","mode":"add","date":day,"msg":msg}
        return await ask_name(msg,ctx)

    if d=="add_sal":
        ctx.user_data["flow"] = {"step":"val","mode":"salary","date":sdate(dt.date.today()),"msg":msg}
        return await ask_amount(msg,ctx)

    # TODAY VIEW
    if d=="go_today":
        ctx.application.bot_data["entries"] = read_sheet()
        today = dt.date.today()
        code = f"{today.year}-{today.month:02d}"
        return await show_day(msg,ctx,code,sdate(today))

    # DELETE
    if d.startswith("drow_"):
        _,rid,code,day = d.split("_",3)
        delete_row(int(rid))
        ctx.application.bot_data["entries"] = read_sheet()
        return await show_day(msg,ctx,code,day)

    # EDIT
    if d.startswith("edit_"):
        _,rid,code,day = d.split("_",3)
        idx = int(rid)
        old = next(e for e in ctx.application.bot_data["entries"].get(code,[]) if e["row_idx"]==idx)
        ctx.user_data["flow"] = {
            "mode":"edit","row":idx,"date":day,
            "old_symbols":old["symbols"],"old_amount":old["amount"],
            "msg":msg
        }
        return await ask_name(msg,ctx)

    # UNDO ADD
    if d.startswith("undo_") and not d.startswith("undo_edit_"):
        idx = int(d.split("_",1)[1])
        ud = ctx.user_data.get("undo",{})
        if ud.get("row")==idx and dt.datetime.utcnow()<=ud.get("expires"):
            delete_row(idx)
            ctx.application.bot_data["entries"] = read_sheet()
            return await show_day(msg,ctx,ud["code"],ud["date"])
        else:
            return await msg.reply_text("⏱ Время вышло")

    # UNDO EDIT
    if d.startswith("undo_edit_"):
        idx = int(d.split("_",1)[1])
        ud = ctx.user_data.get("undo_edit",{})
        if ud.get("row")==idx and dt.datetime.utcnow()<=ud.get("expires"):
            update_row(idx, ud["old_symbols"], ud["old_amount"])
            ctx.application.bot_data["entries"] = read_sheet()
            return await show_day(msg,ctx,ud["code"],ud["date"])
        else:
            return await msg.reply_text("⏱ Время вышло")

    # YEARS / MONTHS / TOGGLE
    if d.startswith("year_"):
        return await show_year(msg,ctx,d.split("_",1)[1])
    if d.startswith("mon_"):
        return await show_month(msg,ctx,d.split("_",1)[1])
    if d.startswith("tgl_"):
        _,code,fl = d.split("_",2)
        return await show_month(msg,ctx,code,fl)

    # PROFIT / HISTORY / KPI
    if d=="profit_now":
        s,e = bounds_today()
        return await show_profit(msg,ctx,s,e,"💰 Текущая ЗП")
    if d=="profit_prev":
        s,e = bounds_prev()
        return await show_profit(msg,ctx,s,e,"💼 Прошлая ЗП")
    if d=="hist":
        return await show_history(msg,ctx)
    if d=="kpi":
        return await show_kpi(msg,ctx,False)
    if d=="kpi_prev":
        return await show_kpi(msg,ctx,True)

# ─── COMMANDS & RUN ─────────────────────────────────────────────────────────
async def cmd_start(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data={"entries":read_sheet(),"chats":set()}
    await update.message.reply_text("<b>📊 Главное меню</b>", parse_mode="HTML", reply_markup=main_kb())
    ctx.application.bot_data["chats"].add(update.effective_chat.id)

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))
    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh, mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))
    logger.info("🚀 Bot up")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()