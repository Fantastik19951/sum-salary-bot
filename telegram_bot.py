import os
import logging
import datetime as dt
import re
from collections import defaultdict

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
UNDO_WINDOW  = 10  # seconds
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
    try: return float(s.replace(",","."))
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
        if sal is not None: e["salary"] = sal
        else:             e["amount"] = amt
        key = f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row = [entry["date"], entry.get("symbols",""),
           entry.get("amount",""), entry.get("salary","")]
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

# ─── UI HELPERS ─────────────────────────────────────────────────────────────
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

def nav_main_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное", callback_data="main")]])

async def safe_edit(msg:Message, text:str, kb=None):
    kb = kb or nav_main_kb()
    try:    return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except: return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

def fmt_amount(x:float)->str:
    if x==int(x): return f"{int(x):,}".replace(",",".")
    s=f"{x:.2f}".rstrip("0").rstrip(".")
    i,f=s.split(".") if "." in s else (s,"")
    return f"{int(i):,}".replace(",",".") + (f and ("," + f))    

def bounds_today():
    d=dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16), d)

def bounds_prev():
    d=dt.date.today()
    if d.day<=15:
        last=d.replace(day=1)-dt.timedelta(days=1)
        return (last.replace(day=16), last)
    return (d.replace(day=1), d.replace(day=15))

# ─── VIEWS ──────────────────────────────────────────────────────────────────
async def show_main(msg, ctx):
    ctx.application.bot_data.setdefault("chats", set()).add(msg.chat_id)
    ctx.application.bot_data["entries"] = read_sheet()
    await safe_edit(msg, "<b>📊 Главное меню</b>", main_kb())

async def show_year(msg, ctx, year):
    btns=[InlineKeyboardButton(MONTH_NAMES[i].capitalize(),
           callback_data=f"mon_{year}-{i+1:02d}") for i in range(12)]
    rows=[btns[i:i+4] for i in range(0,12,4)]
    rows.append([InlineKeyboardButton("🏠 Главное", callback_data="main")])
    await safe_edit(msg, f"<b>📆 {year}</b>", InlineKeyboardMarkup(rows))

async def show_month(msg, ctx, code, flag=None):
    year,mon=code.split("-")
    mname=MONTH_NAMES[int(mon)-1].capitalize()
    today=dt.date.today()
    if flag is None:
        flag="old" if today.strftime("%Y-%m")==code and today.day<=15 else "new"
    ents=ctx.application.bot_data["entries"].get(code,[])
    part=[e for e in ents if "amount" in e and ((pdate(e["date"]).day<=15)==(flag=="old"))]
    days=sorted({e["date"] for e in part}, key=pdate)
    total=sum(e["amount"] for e in part)
    header=f"<b>{mname} {year} · {'01–15' if flag=='old' else '16–31'}</b>"
    body="\n".join(f"{e['date']} · {e['symbols']} · {fmt_amount(e['amount'])} $" for e in part) or "Нет записей"
    footer=f"<b>Итого: {fmt_amount(total)} $</b>"
    togg="new" if flag=="old" else "old"
    rows=[[InlineKeyboardButton("Первая" if flag=="old" else "Вторая",
             callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("🏠 Главное", callback_data="main")])
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_day(msg, ctx, code, date):
    ctx.application.bot_data["entries"] = read_sheet()
    ents=[e for e in ctx.application.bot_data["entries"].get(code,[]) if e["date"]==date and "amount" in e]
    total=sum(e["amount"] for e in ents)
    header=f"<b>{date}</b>"
    body="\n".join(f"{i}. {e['symbols']} · {fmt_amount(e['amount'])} $" for i,e in enumerate(ents,1)) or "Нет записей"
    footer=f"<b>Итого: {fmt_amount(total)} $</b>"
    rows=[
        [
            InlineKeyboardButton(f"❌{i}", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i}", callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ]
        for i,e in enumerate(ents,1)
    ]
    rows.append([InlineKeyboardButton("➕ Добавить", callback_data=f"add_{code}_{date}")])
    rows.append([InlineKeyboardButton("🏠 Главное", callback_data="main")])
    await safe_edit(msg, "\n".join([header, body, "", footer]), InlineKeyboardMarkup(rows))

async def show_history(msg, ctx):
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        text="История пуста"
    else:
        lines=[f"• {pdate(e['date']).day} {MONTH_NAMES[pdate(e['date']).month-1]} {pdate(e['date']).year} — {fmt_amount(e['salary'])} $" for e in sorted(ents,key=lambda x:pdate(x['date']))]
        text="<b>📜 История ЗП</b>\n"+ "\n".join(lines)
    await safe_edit(msg, text, nav_main_kb())

async def show_profit(msg, ctx, start, end, title):
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v if start<=pdate(e['date'])<=end and "amount" in e]
    tot=sum(e["amount"] for e in ents)
    text=f"{title} ({sdate(start)} – {sdate(end)})\n<b>10%: {fmt_amount(tot*0.10)} $</b>"
    await safe_edit(msg, text, nav_main_kb())

async def show_kpi(msg, ctx, prev=False):
    if prev:
        start,end=bounds_prev(); title="📊 KPI прошлого"; 
    else:
        start,end=bounds_today(); title="📊 KPI текущего"
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v if start<=pdate(e['date'])<=end and "amount" in e]
    if not ents:
        return await safe_edit(msg,"Нет данных",nav_main_kb())
    turn=sum(e["amount"] for e in ents)
    sal=turn*0.10; days=len({e['date'] for e in ents}); plen=(end-start).days+1; avg=sal/days if days else 0
    text=(
        f"{title} ({sdate(start)} – {sdate(end)})\n"
        f"• Оборот: {fmt_amount(turn)} $\n"
        f"• ЗП10%: {fmt_amount(sal)} $\n"
        f"• Дней: {days}/{plen}\n"
        f"• Ср/день: {fmt_amount(avg)} $"
    )
    await safe_edit(msg, text, nav_main_kb())

# ─── ADD / EDIT FLOW ─────────────────────────────────────────────────────────
async def ask_date(msg, ctx):
    prompt = await msg.reply_text(
        "📅 Введите дату (ДД.MM.YYYY) или «Сегодня»",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня",callback_data="today_add")]])
    )
    ctx.user_data["flow"] = {"step":"date", "prompt":prompt, "msg":msg}

async def ask_name(msg, ctx):
    prompt = await msg.reply_text("✏️ Введите имя:")
    ctx.user_data["flow"]["step"]="sym"
    ctx.user_data["flow"]["prompt"]=prompt

async def ask_amount(msg, ctx, prev=None):
    text = "💰 Введите сумму:"
    if prev is not None:
        text = f"💰 Введите новую сумму (старое: {fmt_amount(prev)} $):"
    prompt = await msg.reply_text(text)
    ctx.user_data["flow"]["step"]="val"
    ctx.user_data["flow"]["prompt"]=prompt

async def process_text(u:Update, ctx:ContextTypes.DEFAULT_TYPE):
    flow = ctx.user_data.get("flow")
    if not flow:
        return
    txt=u.message.text.strip()
    try: await u.message.delete()
    except: pass
    try: await flow["prompt"].delete()
    except: pass

    # DATE
    if flow["step"]=="date":
        if txt.lower() in ("сегодня","today"):
            flow["date"]=sdate(dt.date.today())
        elif is_date(txt):
            flow["date"]=txt
        else:
            return await u.message.reply_text("Неверный формат даты")
        return await ask_name(flow["msg"], ctx)

    # NAME
    if flow["step"]=="sym":
        flow["symbols"]=txt
        # if edit, store old
        if flow.get("mode")=="edit":
            idx=flow["row"]
            old = next(e for e in ctx.application.bot_data["entries"]
                       .get(flow["date"][:7],[]) if e["row_idx"]==idx)
            flow["old_amount"]=old["amount"]
        return await ask_amount(flow["msg"], ctx, flow.get("old_amount"))

    # AMOUNT
    if flow["step"]=="val":
        try: val=float(txt.replace(",","."))
        except: return await u.message.reply_text("Нужно число")
        date=flow["date"]; code=date[:7]
        # EDIT
        if flow.get("mode")=="edit":
            idx=flow["row"]
            update_row(idx, flow["symbols"], val)
            ctx.application.bot_data["entries"]=read_sheet()
            # confirm
            resp=await flow["msg"].reply_text(
                "✅ Данные заменены",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↺ Отменить",callback_data=f"undo_edit_{idx}")
                ]])
            )
            ctx.user_data["undo_edit"]={
                "row":idx,
                "old_symbols":flow["old_symbols"],
                "old_amount":flow["old_amount"],
                "expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)
            }
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(resp.chat.id,resp.message_id),
                when=UNDO_WINDOW
            )
            ctx.user_data.pop("flow")
            return await show_day(flow["msg"],ctx,code,date)

        # ADD
        flow["amount"]=val
        row=push_row(flow)
        ctx.application.bot_data["entries"]=read_sheet()
        resp=await flow["msg"].reply_text(
            f"✅ Добавлено: {flow['symbols']} · {fmt_amount(val)} $",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить",callback_data=f"undo_{row}")
            ]])
        )
        ctx.user_data["undo"]={
            "row":row,
            "expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)
        }
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id,resp.message_id),
            when=UNDO_WINDOW
        )
        ctx.user_data.pop("flow")
        return await show_day(flow["msg"],ctx,code,date)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd:Update, ctx:ContextTypes.DEFAULT_TYPE):
    q=upd.callback_query
    if not q: return
    data, msg = q.data, q.message
    await q.answer()

    if data=="main":
        return await show_main(msg,ctx)

    if data=="today_add":
        ctx.user_data["flow"]={"step":"date","msg":msg}
        return await process_text(upd,ctx)

    if data=="add_rec":
        return await ask_date(msg,ctx)

    if data.startswith("add_"):
        _,code,date=data.split("_",2)
        ctx.user_data["flow"]={"step":"sym","mode":"add","date":date,"msg":msg}
        return await ask_name(msg,ctx)

    if data=="add_sal":
        ctx.user_data["flow"]={"step":"val","mode":"salary","date":sdate(dt.date.today()),"msg":msg}
        return await ask_amount(msg,ctx)

    if data.startswith("year_"):
        return await show_year(msg,ctx,data.split("_",1)[1])

    if data.startswith("mon_"):
        return await show_month(msg,ctx,data.split("_",1)[1])

    if data.startswith("tgl_"):
        _,code,fl=data.split("_",2)
        return await show_month(msg,ctx,code,fl)

    if data.startswith("day_"):
        _,code,day=data.split("_",2)
        return await show_day(msg,ctx,code,day)

    if data=="go_today":
        d=sdate(dt.date.today()); c=d[:7]
        return await show_day(msg,ctx,c,d)

    if data.startswith("drow_"):
        _,row,code,day=data.split("_",4)[:4]
        delete_row(int(row))
        ctx.application.bot_data["entries"]=read_sheet()
        await msg.reply_text("🚫 Удалено")
        return await show_day(msg,ctx,code,day)

    if data.startswith("edit_"):
        _,row,code,day=data.split("_",4)[:4]
        row=int(row)
        old=next(e for e in ctx.application.bot_data["entries"].get(code,[]) if e["row_idx"]==row)
        ctx.user_data["flow"]={
            "step":"sym","mode":"edit","row":row,"date":day,
            "old_symbols":old["symbols"],"msg":msg
        }
        return await ask_name(msg,ctx)

    if data.startswith("undo_"):
        row=int(data.split("_",1)[1])
        u=ctx.user_data.get("undo",{})
        if u.get("row")==row and dt.datetime.utcnow()<=u.get("expires"):
            delete_row(row); ctx.application.bot_data["entries"]=read_sheet()
            await msg.reply_text("↺ Добавление отменено")
            # no need to refresh day: user can tap again
        else:
            await msg.reply_text("⏱ Время вышло")
        return

    if data.startswith("undo_edit_"):
        row=int(data.split("_",1)[1])
        u=ctx.user_data.get("undo_edit",{})
        if u.get("row")==row and dt.datetime.utcnow()<=u.get("expires"):
            update_row(row,u["old_symbols"],u["old_amount"])
            ctx.application.bot_data["entries"]=read_sheet()
            await msg.reply_text("↺ Изменение отменено")
        else:
            await msg.reply_text("⏱ Время вышло")
        return

    if data=="profit_now":
        s,e=bounds_today()
        return await show_profit(msg,ctx,s,e,"💰 Текущая ЗП")

    if data=="profit_prev":
        s,e=bounds_prev()
        return await show_profit(msg,ctx,s,e,"💼 Прошлая ЗП")

    if data=="hist":
        return await show_history(msg,ctx)

    if data=="kpi":
        return await show_kpi(msg,ctx,False)

    if data=="kpi_prev":
        return await show_kpi(msg,ctx,True)

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"]=read_sheet()
    await show_main(update.message,ctx)

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.bot_data={"entries":read_sheet()}

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    app.job_queue.run_repeating(auto_sync,5,0)
    hh,mm=REMIND_HH_MM
    app.job_queue.run_daily(reminder,time=dt.time(hour=hh,minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()