import os
import logging
import datetime as dt
import re
from collections import defaultdict
from io import BytesIO
import csv

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Message
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & LOGGING ───────────────────────────────────────────────────────
load_dotenv()
if not os.path.exists("credentials.json"):
    env = os.getenv("GOOGLE_KEY_JSON")
    if env:
        with open("credentials.json","w") as f: f.write(env)
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10   # seconds
REMIND_HH_MM = (20,0)
MONTH_NAMES  = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря"
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# ─── GOOGLE SHEETS ----------------------------------------------------------
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
def pdate(s): return dt.datetime.strptime(s,DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))

def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx,row in enumerate(SHEET.get_all_values(),start=1):
        if idx<=HEADER_ROWS or len(row)<2: continue
        d=row[0].strip()
        if not is_date(d): continue
        e={"date":d,"symbols":row[1].strip(),"row_idx":idx}
        amt = safe_float(row[2]) if len(row)>2 else None
        sal = safe_float(row[3]) if len(row)>3 else None
        if amt is None and sal is None: continue
        if sal is not None: e["salary"]=sal
        else:             e["amount"]=amt
        key=f"{pdate(d).year}-{pdate(d).month:02d}"
        data[key].append(e)
    return data

def push_row(entry):
    if not SHEET: return None
    nd = pdate(entry["date"])
    row=[entry["date"], entry.get("symbols",""),
         entry.get("amount",""), entry.get("salary","")]
    col=SHEET.col_values(1)[HEADER_ROWS:]
    ins=HEADER_ROWS
    for i,v in enumerate(col,start=HEADER_ROWS+1):
        try: dv=pdate(v.strip())
        except: continue
        if dv<=nd: ins=i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

def delete_row(idx):
    if SHEET: SHEET.delete_rows(idx)

# ─── SYNC & REMINDER ────────────────────────────────────────────────────────
async def auto_sync(ctx: ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"]=read_sheet()

async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    for cid in ctx.application.bot_data.get("chats",set()):
        try: await ctx.bot.send_message(cid,"⏰ Не забудьте внести записи сегодня!")
        except: pass

# ─── UI HELPERS ─────────────────────────────────────────────────────────────
def nav_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Главное", callback_data="main")
    ]])

async def safe_edit(msg: Message, text: str, kb=None):
    kb=kb or nav_kb()
    try: return await msg.edit_text(text,parse_mode="HTML",reply_markup=kb)
    except: return await msg.reply_text(text,parse_mode="HTML",reply_markup=kb)

# ─── BOUNDARIES ─────────────────────────────────────────────────────────────
def bounds_today():
    d=dt.date.today()
    start=d.replace(day=1) if d.day<=15 else d.replace(day=16)
    return start,d

def bounds_prev():
    d=dt.date.today()
    if d.day<=15:
        last=(d.replace(day=1)-dt.timedelta(days=1))
        return last.replace(day=16), last
    else:
        return d.replace(day=1), d.replace(day=15)

# ─── MAIN MENU ──────────────────────────────────────────────────────────────
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024",    callback_data="year_2024"),
         InlineKeyboardButton("📅 2025",    callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("➕ Запись",   callback_data="add_rec")],
        [InlineKeyboardButton("💵 Зарплата",callback_data="add_sal")],
        [InlineKeyboardButton("💰 Текущая ЗП", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлая ЗП", callback_data="profit_prev")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist"),
         InlineKeyboardButton("🗄 Экспорт",    callback_data="export_now")],
        [InlineKeyboardButton("📊 KPI тек.",    callback_data="kpi"),
         InlineKeyboardButton("📊 KPI прошл.",  callback_data="kpi_prev")],
    ])

async def show_main(msg: Message, ctx: ContextTypes.DEFAULT_TYPE):
    await safe_edit(msg, "<b>📊 Главное меню</b>", main_kb())

# ─── YEAR → MONTH → DAY ─────────────────────────────────────────────────────
def year_kb(year):
    btns=[InlineKeyboardButton(MONTH_NAMES[i].capitalize(),callback_data=f"mon_{year}-{i+1:02d}")
          for i in range(12)]
    rows=[btns[i:i+4] for i in range(0,12,4)]
    rows.append([InlineKeyboardButton("⬅️ Назад",callback_data="back"),
                 InlineKeyboardButton("🏠 Главное",callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_year(msg,ctx,year):
    await safe_edit(msg,f"<b>📆 {year}</b>",year_kb(year))

def month_kb(code,flag,days):
    togg="old" if flag=="new" else "new"
    rows=[[InlineKeyboardButton("Первая" if flag=="old" else "Вторая",
                                callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d,callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("⬅️ Назад",callback_data="back"),
                 InlineKeyboardButton("🏠 Главное",callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_month(msg,ctx,code,flag=None):
    today=dt.date.today()
    if flag is None:
        flag="old" if today.strftime("%Y-%m")==code and today.day<=15 else "new"
    ents=ctx.application.bot_data["entries"].get(code,[])
    part=[e for e in ents if "amount" in e and (pdate(e["date"]).day<=15)==(flag=="old")]
    days=sorted({e["date"] for e in part},key=pdate)
    total=sum(e["amount"] for e in part)
    lines=[f"{i}. {e['date']} · {e['symbols']} · {e['amount']}"
           for i,e in enumerate(part)]
    body="\n".join(lines) or "Нет записей"
    await safe_edit(msg,
        f"<b>{code} · {'01–15' if flag=='old' else '16–31'}</b>\n"
        f"{body}\n\n<b>Итого:</b> {total}",
        month_kb(code,flag,days)
    )

def day_kb(code,date,ents):
    rows=[]
    for i,e in enumerate(ents):
        rows.append([
            InlineKeyboardButton(f"❌{i}",callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i}",callback_data=f"edit_{e['row_idx']}_{code}_{date}")
        ])
    rows.append([InlineKeyboardButton("➕ Добавить",callback_data=f"add_{code}_{date}")])
    rows.append([InlineKeyboardButton("⬅️ Назад",callback_data="back"),
                 InlineKeyboardButton("🏠 Главное",callback_data="main")])
    return InlineKeyboardMarkup(rows)

async def show_day(msg,ctx,code,date):
    ents=[e for e in ctx.application.bot_data["entries"].get(code,[])
          if e["date"]==date and "amount" in e]
    total=sum(e["amount"] for e in ents)
    lines=[f"{i}. {e['symbols']} · {e['amount']}" for i,e in enumerate(ents)]
    body="\n".join(lines) or "Нет записей"
    await safe_edit(msg,
        f"<b>{date}</b>\n{body}\n\n<b>Итого:</b> {total}",
        day_kb(code,date,ents)
    )

# ─── EXPORT CURRENT MONTH & SEARCH ──────────────────────────────────────────
async def export_current(ctx,msg):
    code = dt.date.today().strftime("%Y-%m")
    ents = ctx.application.bot_data["entries"].get(code,[])
    if not ents:
        return await msg.reply_text("Нет данных за текущий месяц")
    buf=BytesIO(); w=csv.writer(buf)
    w.writerow(["Дата","Имя","Сумма"])
    for e in ents:
        w.writerow([e["date"],e["symbols"],e.get("amount") or e.get("salary") or 0])
    buf.seek(0)
    await msg.reply_document(document=buf,filename=f"export_{code}.csv")

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # leave as before or implement as needed
    await update.message.reply_text("Используйте /export или меню")

# ─── ADD FLOW WITH AUTO-HIDE PROMPTS & UNDO ─────────────────────────────────
async def ask_rec(msg,ctx):
    # ask date
    m = await msg.reply_text("📅 Введите дату (ДД.MM.ГГГГ) или сегодня:")
    ctx.user_data["flow"]={"step":"date","prompt":m}
async def ask_sal(msg,ctx):
    m = await msg.reply_text("💰 Введите сумму:")
    ctx.user_data["flow"]={"step":"val","mode":"salary","date":sdate(dt.date.today()),"prompt":m}

async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    flow=ctx.user_data.get("flow")
    if not flow: return
    txt=u.message.text.strip()
    try: await u.message.delete()
    except: pass
    # remove prompt
    try: await flow["prompt"].delete()
    except: pass

    if flow["step"]=="date":
        if txt.lower() in ("сегодня","today"):
            flow["date"]=sdate(dt.date.today())
        elif is_date(txt):
            flow["date"]=txt
        else:
            return await u.message.reply_text("Неверный формат даты")
        flow["step"]="sym"
        m=await u.message.reply_text("✏️ Введите имя:")
        flow["prompt"]=m
        return

    if flow["step"]=="sym":
        flow["symbols"]=txt
        flow["step"]="val"
        m=await u.message.reply_text("💰 Введите сумму:")
        flow["prompt"]=m
        return

    if flow["step"]=="val":
        try: val=float(txt.replace(",",".")) 
        except: return await u.message.reply_text("Нужно число")
        if flow.get("mode")=="salary": flow["salary"]=val
        else:                         flow["amount"]=val
        row=push_row(flow)
        ctx.application.bot_data["entries"]=read_sheet()
        ctx.user_data.pop("flow",None)
        # confirmation + undo button
        resp=await u.message.reply_html(
            f"✅ Добавлено: <b>{flow['symbols']}</b> — <b>{val}</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить",callback_data=f"undo_{row}")
            ]])
        )
        ctx.user_data["undo"]={"row":row,"expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)}
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(resp.chat.id,resp.message_id),
            when=UNDO_WINDOW
        )

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=upd.callback_query
    if not q: return
    data, msg = q.data, q.message
    await q.answer()

    if data in ("main","back"):
        return await show_main(msg,ctx)
    if data=="refresh":
        return await show_main(msg,ctx)
    if data=="go_today":
        code=dt.date.today().strftime("%Y-%m"); day=sdate(dt.date.today())
        return await show_day(msg,ctx,code,day)
    if data=="year_2024": return await show_year(msg,ctx,"2024")
    if data=="year_2025": return await show_year(msg,ctx,"2025")
    if data.startswith("mon_"):
        return await show_month(msg,ctx,data.split("_",1)[1])
    if data.startswith("tgl_"):
        _,code,fl=data.split("_",2); return await show_month(msg,ctx,code,fl)
    if data.startswith("day_"):
        _,code,day=data.split("_",2); return await show_day(msg,ctx,code,day)
    if data=="add_rec":
        return await ask_rec(msg,ctx)
    if data=="add_sal":
        return await ask_sal(msg,ctx)
    if data.startswith("drow_"):
        _,row,code,day=data.split("_",3)
        delete_row(int(row)); ctx.application.bot_data["entries"]=read_sheet()
        r=await msg.reply_text("🚫 Удалено")
        ctx.application.job_queue.run_once(lambda c:c.bot.delete_message(r.chat.id,r.message_id),when=UNDO_WINDOW)
        return await show_day(msg,ctx,code,day)
    if data.startswith("undo_"):
        row=int(data.split("_",1)[1]); u=ctx.user_data.get("undo",{})
        if u.get("row")==row and dt.datetime.utcnow()<=u.get("expires",dt.datetime.min):
            delete_row(row); ctx.application.bot_data["entries"]=read_sheet()
            try: await msg.delete()
            except: pass
            r=await msg.reply_text("↺ Отменено")
            ctx.application.job_queue.run_once(lambda c:c.bot.delete_message(r.chat.id,r.message_id),when=UNDO_WINDOW)
        else:
            await msg.reply_text("⏱ Время вышло")
        return
    if data.startswith("edit_"):
        _,row,code,day=data.split("_",3); row=int(row)
        entry=next(e for e in ctx.application.bot_data["entries"].get(code,[]) if e["row_idx"]==row)
        ctx.user_data["flow"]={
            "step":"sym","date":day,
            "symbols":entry["symbols"],"mode":"edit","row":row
        }
        return await msg.reply_text(f"✏️ Новое имя (было {entry['symbols']}):")
    if data=="profit_now":
        s,e=bounds_today(); return await show_profit(msg,ctx,s,e,"💰 Текущая ЗП")
    if data=="profit_prev":
        s,e=bounds_prev(); return await show_profit(msg,ctx,s,e,"💼 Прошлая ЗП")
    if data=="hist": return await show_history(msg,ctx)
    if data=="export_now":
        return await export_current(ctx,msg)
    if data=="kpi": return await show_kpi(msg,ctx,False)
    if data=="kpi_prev": return await show_kpi(msg,ctx,True)

# ─── HISTORY & KPI & PROFIT ─────────────────────────────────────────────────
async def show_history(msg,ctx):
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v if "salary" in e]
    if not ents:
        return await safe_edit(msg,"История пуста",nav_kb())
    lines=[]
    for e in sorted(ents,key=lambda x:pdate(x["date"])):
        d=pdate(e["date"])
        lines.append(f"{d.day} {MONTH_NAMES[d.month-1]} {d.year} — {e['salary']}")
    body="\n".join(lines)
    await safe_edit(msg,f"<b>📜 История зарплат</b>\n{body}",nav_kb())

async def show_profit(msg,ctx,start,end,title):
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v
          if start<=pdate(e["date"])<=end and "amount" in e]
    tot=sum(e["amount"] for e in ents)
    await safe_edit(msg,f"{title}\n<b>10 %: {round(tot*0.10,2)}</b>",nav_kb())

async def show_kpi(msg,ctx,prev=False):
    if prev:
        start,end=bounds_prev(); title="📊 KPI прошл."
    else:
        start,end=bounds_today(); title="📊 KPI тек."
    ents=[e for v in ctx.application.bot_data["entries"].values() for e in v
          if start<=pdate(e["date"])<=end and "amount" in e]
    if not ents:
        return await safe_edit(msg,"Нет данных",nav_kb())
    turn=sum(e["amount"] for e in ents)
    sal=round(turn*0.10,2)
    days=len({e["date"] for e in ents})
    plen=(end-start).days+1
    avg=round(sal/days,2) if days else 0
    await safe_edit(msg,f"{title}\n• Оброт: {turn}\n• ЗП: {sal}\n• {days}/{plen} дней\n• Ср/день: {avg}",nav_kb())

# ─── START & RUN ────────────────────────────────────────────────────────────
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data.setdefault("chats",set()).add(u.effective_chat.id)
    ctx.application.bot_data["entries"]=read_sheet()
    await show_main(u.message,ctx)

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.bot_data={}
    app.bot_data["entries"]=read_sheet()

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("export",lambda u,c:export_current(c,u.message)))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,process_text))

    app.job_queue.run_repeating(auto_sync,interval=5,first=0)
    hh,mm=REMIND_HH_MM
    app.job_queue.run_daily(reminder,time=dt.time(hour=hh,minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()