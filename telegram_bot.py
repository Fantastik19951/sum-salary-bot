import os, logging, datetime as dt, re
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG & LOGGING ─────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_JSON = os.getenv("GOOGLE_KEY_JSON")
if not TOKEN: raise RuntimeError("TELEGRAM_BOT_TOKEN must be set")
if GOOGLE_JSON and not os.path.exists("credentials.json"):
    with open("credentials.json","w") as f: f.write(GOOGLE_JSON)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─── SHEETS ────────────────────────────────────────────
scope = ["https://spreadsheets.google.com/feeds","https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive.file","https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
sheet = gspread.authorize(creds).open("TelegramBotData").sheet1

DATE_FMT="%d.%m.%Y"
DATE_RX=re.compile(r"\d{2}\.\d{2}\.\d{4}$")
def sdate(d): return d.strftime(DATE_FMT)
def pdate(s): return dt.datetime.strptime(s,DATE_FMT).date()
def is_date(s): return bool(DATE_RX.fullmatch(s))

def read_entries():
    data=defaultdict(list)
    rows=sheet.get_all_values()
    for i,row in enumerate(rows[4:], start=5):
        if not row[0] or not is_date(row[0]): continue
        amt = row[2]
        if not amt: continue
        date,row_sym,row_amt = row[0],row[1],float(amt.replace(",","."))
        key=date
        data[key].append({"row":i,"sym":row_sym,"amt":row_amt})
    return data

def insert_entry(date, sym, amt):
    all_dates = sheet.col_values(1)[4:]
    pos = 5
    nd = pdate(date)
    for idx,d in enumerate(all_dates, start=5):
        try:
            if pdate(d) <= nd:
                pos = idx+1
            else:
                break
        except: pass
    sheet.insert_row([date, sym, amt], pos, value_input_option="USER_ENTERED")
    return

def update_entry(row, sym, amt):
    sheet.update_cell(row,2,sym)
    sheet.update_cell(row,3,amt)

def delete_entry(row):
    sheet.delete_rows(row)

# ─── UI ─────────────────────────────────────────────────
PAD=" "  # &nbsp;
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{PAD}📆 Сегодня{PAD}", callback_data="day_today")],
        [InlineKeyboardButton(f"{PAD}➕ Добавить{PAD}", callback_data="add_today")]
    ])

def day_kb(entries):
    kb=[]
    for idx,e in enumerate(entries,1):
        kb.append([
            InlineKeyboardButton(f"✏️{idx}", callback_data=f"edit_{e['row']}"),
            InlineKeyboardButton(f"❌{idx}", callback_data=f"del_{e['row']}")
        ])
    kb.append([InlineKeyboardButton("🏠 Главное", callback_data="main")])
    return InlineKeyboardMarkup(kb)

# ─── HANDLERS ────────────────────────────────────────────
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ctx.application.bot_data["entries"]=read_entries()
    await u.message.reply_text("📊 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_kb())

async def cb(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=u.callback_query; await q.answer()
    data=q.data
    today=sdate(dt.date.today())
    entries_map = read_entries()
    if data=="main":
        await q.message.edit_text("📊 <b>Главное меню</b>", parse_mode="HTML", reply_markup=main_kb())
        return
    if data=="day_today":
        ents=entries_map.get(today,[])
        text = f"<b>{today}</b>\n" + "\n".join(f"{i+1}. {e['sym']} · {e['amt']}" for i,e in enumerate(ents)) or "Нет записей"
        await q.message.edit_text(text, parse_mode="HTML", reply_markup=day_kb(ents))
        return
    if data=="add_today":
        ctx.user_data["flow"]={"step":"add_sym","date":today}
        await q.message.reply_text("✏️ Введите имя:")
        return
    if data.startswith("edit_"):
        row=int(data.split("_",1)[1])
        # find entry
        for e in entries_map.get(today,[]):
            if e["row"]==row:
                ctx.user_data["flow"]={"step":"edit_sym","row":row,"date":today}
                await q.message.reply_text(f"✏️ Новое имя (текущее: {e['sym']}):")
                return
    if data.startswith("del_"):
        row=int(data.split("_",1)[1])
        delete_entry(row)
        # сразу обновляем окно
        ents=read_entries().get(today,[])
        text = f"<b>{today}</b>\n" + "\n".join(f"{i+1}. {e['sym']} · {e['amt']}" for i,e in enumerate(ents)) or "Нет записей"
        await q.message.edit_text(text, parse_mode="HTML", reply_markup=day_kb(ents))
        return

async def msg_handler(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    flow=ctx.user_data.get("flow")
    if not flow: return
    text=u.message.text.strip()
    step=flow["step"]; date=flow["date"]
    await u.message.delete()
    if step=="add_sym":
        flow["sym"]=text; flow["step"]="add_amt"
        return await u.message.reply_text("💰 Введите сумму:")
    if step=="add_amt":
        try: amt=float(text.replace(",","."))
        except: return await u.message.reply_text("Нужно число")
        insert_entry(date, flow["sym"], amt)
        # обновляем
        ents=read_entries().get(date,[])
        txt=f"<b>{date}</b>\n"+"\n".join(f"{i+1}. {e['sym']} · {e['amt']}" for i,e in enumerate(ents)) or "Нет записей"
        await u.message.reply_text("✅ Добавлено", reply_markup=day_kb(ents))
        return
    if step=="edit_sym":
        flow["sym_new"]=text; flow["step"]="edit_amt"
        return await u.message.reply_text("💰 Введите новую сумму:")
    if step=="edit_amt":
        try: amt=float(text.replace(",","."))
        except: return await u.message.reply_text("Нужно число")
        update_entry(flow["row"], flow["sym_new"], amt)
        ents=read_entries().get(date,[])
        txt=f"<b>{date}</b>\n"+"\n".join(f"{i+1}. {e['sym']} · {e['amt']}" for i,e in enumerate(ents)) or "Нет записей"
        await u.message.reply_text("✅ Изменено", reply_markup=day_kb(ents))
        return

# ─── RUN ────────────────────────────────────────────────────────
if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler))
    logger.info("Бот запущен")
    Bot(TOKEN).delete_webhook(drop_pending_updates=True)
    app.run_polling(drop_pending_updates=True)