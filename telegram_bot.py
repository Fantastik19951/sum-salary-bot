import os, logging, datetime as dt, re
from collections import defaultdict, deque

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── SETTINGS ───────────────────────────────────────────────────────────────
load_dotenv()
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT    = "%d.%m.%Y"
DATE_RX     = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS = 4
REMIND_HH_MM= (20,0)
UNDO_SEC    = 10
MONTHS      = ["января","февраля","марта","апреля","мая","июня",
               "июля","августа","сентября","октября","ноября","декабря"]
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ─── SHEETS I/O ─────────────────────────────────────────────────────────────
def connect_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive.file",
             "https://www.googleapis.com/auth/drive"]
    creds=ServiceAccountCredentials.from_json_keyfile_name("credentials.json",scope)
    return gspread.authorize(creds).open("TelegramBotData").sheet1

try: SHEET=connect_sheet()
except Exception as e:
    logging.error("Sheets fail: %s",e); SHEET=None

def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s,DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s.strip()))

def read_data():
    D=defaultdict(list)
    if not SHEET: return D
    for i,row in enumerate(SHEET.get_all_values(),1):
        if i<=HEADER_ROWS or len(row)<3: continue
        d=row[0].strip()
        if not is_date(d): continue
        try: a=float(row[2].replace(",","."))
        except: continue
        D[f"{pdate(d).year}-{pdate(d).month:02d}"].append({
            "date":d, "symbols":row[1].strip(), "amount":a, "row":i
        })
    return D

def push_row(e):
    if not SHEET: return None
    nd=pdate(e["date"])
    row=[e["date"],e["symbols"],e["amount"],""]
    col=SHEET.col_values(1)[HEADER_ROWS:]
    ins=HEADER_ROWS
    for idx,v in enumerate(col,HEADER_ROWS+1):
        try:
            if pdate(v)<=nd: ins=idx
            else: break
        except: pass
    SHEET.insert_row(row,ins+1,value_input_option="USER_ENTERED")
    return ins+1

def update_row(idx,sym,amt):
    if not SHEET: return
    SHEET.update_cell(idx,2,sym)
    SHEET.update_cell(idx,3,amt)

def delete_row(idx):
    if SHEET: SHEET.delete_rows(idx)

# ─── SYNC & REMIND ──────────────────────────────────────────────────────────
async def auto_sync(ctx): ctx.application.bot_data["D"]=read_data()
async def reminder(ctx):
    for cid in ctx.application.bot_data.get("chats",()):
        try: await ctx.bot.send_message(cid,"⏰ Не забудьте внести записи!")
        except: pass

# ─── NAVIGATION ─────────────────────────────────────────────────────────────
def init_nav(ctx): ctx.user_data["nav"]=deque([("main","Главное меню")])
def push_nav(ctx,code,label): ctx.user_data["nav"].append((code,label))
def pop_nav(ctx):
    nav=ctx.user_data["nav"]
    if len(nav)>1: nav.pop()
    return nav[-1]

# ─── UI ─────────────────────────────────────────────────────────────────────
def main_kb():
    p=" "
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p*8}📆 Сегодня{p*8}","go_today"),
         InlineKeyboardButton(f"{p*8}➕ Добавить{p*8}","add")],
        [InlineKeyboardButton("🏠 Главное","main")]
    ])

def back_kb(ctx):
    code,label=pop_nav(ctx)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬅️ {label}",code),
         InlineKeyboardButton("🏠 Главное","main")]
    ])

async def show_main(msg,ctx):
    init_nav(ctx)
    ctx.application.bot_data.setdefault("chats",set()).add(msg.chat.id)
    ctx.application.bot_data["D"]=read_data()
    await msg.edit_text("📊 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_kb())

async def show_day(msg,ctx,date):
    code=date[:7]
    push_nav(ctx,f"day_{code}_{date}",date)
    D=ctx.application.bot_data["D"].get(code,[])
    items=[e for e in D if e["date"]==date]
    text="\n".join(f"{i+1}. {e['symbols']} — {e['amount']}" for i,e in enumerate(items)) or "Нет записей"
    kb=[]
    for i,e in enumerate(items):
        kb.append([
            InlineKeyboardButton(f"❌{i+1}",f"del_{e['row']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️{i+1}",f"edit_{e['row']}_{code}_{date}")
        ])
    kb.append([InlineKeyboardButton("➕ Добавить",f"add_{code}_{date}")])
    kb.append([InlineKeyboardButton("🏠 Главное","main")])
    await msg.edit_text(f"<b>{date}</b>\n\n{text}",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(kb))

# ─── ADD / EDIT FLOW ─────────────────────────────────────────────────────────
async def ask_date(msg,ctx):
    ctx.user_data["flow"]={"step":"date","msg":msg}
    return await msg.reply_text("Введите дату (ДД.MM.YYYY) или «Сегодня»")

async def ask_name(msg,ctx):
    flow=ctx.user_data["flow"]
    text="Введите имя"
    if flow.get("mode")=="edit":
        text+=f" (старое: {flow['symbols']})"
    return await msg.reply_text(text)

async def ask_amt(msg,ctx):
    flow=ctx.user_data["flow"]
    text="Введите сумму"
    if flow.get("mode")=="edit":
        text+=f" (старое: {flow['amount']})"
    return await msg.reply_text(text)

async def process_text(u,ctx):
    flow=ctx.user_data.get("flow")
    if not flow: return
    txt=u.message.text.strip()
    await u.message.delete()
    step=flow["step"]; msg=flow["msg"]

    if step=="date":
        if txt.lower()=="сегодня": date=sdate(dt.date.today())
        elif is_date(txt):       date=txt
        else: return await msg.reply_text("Неверный формат")
        flow.update({"date":date,"step":"sym"})
        return await ask_name(msg,ctx)

    if step=="sym":
        flow.update({"symbols":txt,"step":"amt"})
        return await ask_amt(msg,ctx)

    if step=="amt":
        try: amt=float(txt.replace(",",".")) 
        except: return await msg.reply_text("Нужно число")
        date, sym = flow["date"], flow["symbols"]
        code=date[:7]

        # EDIT
        if flow.get("mode")=="edit":
            idx=flow["row"]
            update_row(idx,sym,amt)
            ctx.application.bot_data["D"]=read_data()
            note=await msg.reply_text("✅ Обновлено",reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↺ Отменить",f"undo_edit_{idx}")
            ]]))
            ctx.application.job_queue.run_once(
                lambda c: c.bot.delete_message(note.chat.id,note.message_id),
                when=UNDO_SEC
            )
            ctx.user_data.pop("flow")
            return await show_day(msg,ctx,date)

        # ADD
        idx=push_row({"date":date,"symbols":sym,"amount":amt})
        ctx.application.bot_data["D"]=read_data()
        note=await msg.reply_text(f"✅ Добавлено: {sym} — {amt}",reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↺ Отменить",f"undo_{idx}")
        ]]))
        ctx.application.job_queue.run_once(
            lambda c: c.bot.delete_message(note.chat.id,note.message_id),
            when=UNDO_SEC
        )
        ctx.user_data.pop("flow")
        return await show_day(msg,ctx,date)

# ─── CALLBACK HANDLER ───────────────────────────────────────────────────────
async def cb(upd,ctx):
    q=upd.callback_query; data, msg = q.data, q.message
    await q.answer()

    if data=="main":        return await show_main(msg,ctx)
    if data=="go_today":    return await show_day(msg,ctx,sdate(dt.date.today()))
    if data=="add":         return await ask_date(msg,ctx)
    if data.startswith("add_"):
        _,code,date = data.split("_",2)
        ctx.user_data["flow"]={"step":"sym","mode":"add","date":date,"msg":msg}
        return await ask_name(msg,ctx)
    if data.startswith("del_"):
        _,r,code,date = data.split("_",3)
        delete_row(int(r)); ctx.application.bot_data["D"]=read_data()
        return await show_day(msg,ctx,date)
    if data.startswith("edit_"):
        _,r,code,date = data.split("_",3); r=int(r)
        old=next(e for e in ctx.application.bot_data["D"][code] if e["row"]==r)
        ctx.user_data["flow"]={
            "step":"sym","mode":"edit","row":r,
            "date":date,"symbols":old["symbols"],"amount":old["amount"],"msg":msg
        }
        return await ask_name(msg,ctx)
    if data.startswith("undo_"):
        _,r = data.split("_",1); delete_row(int(r))
        ctx.application.bot_data["D"]=read_data()
        return await show_main(msg,ctx)
    if data.startswith("undo_edit_"):
        # для простоты - просто перепризовём свежие данные
        ctx.application.bot_data["D"]=read_data()
        return await show_main(msg,ctx)

# ─── START & RUN ────────────────────────────────────────────────────────────
async def start_cmd(u,ctx):
    ctx.application.bot_data={"chats":set(),"D":read_data()}
    await show_main(u.message,ctx)

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",start_cmd))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT&~filters.COMMAND,process_text))
    app.job_queue.run_repeating(auto_sync,interval=5,first=0)
    hh,mm=REMIND_HH_MM
    app.job_queue.run_daily(reminder,time=dt.time(hour=hh,minute=mm))
    logging.info("🚀 Bot up")
    app.run_polling()