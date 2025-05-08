import os
import logging
import datetime as dt
import re
from collections import deque, defaultdict
from io import StringIO
import csv

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    Update
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

TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 10      # секунды для отмены и удаления уведомлений
REMIND_HH_MM = (20, 0)  # 20:00
MONTH_FULL   = ('Январь','Февраль','Март','Апрель','Май','Июнь',
                'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')

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
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
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
    for i, v in enumerate(col, start=HEADER_ROWS+1):
        try:
            d = pdate(v.strip())
        except:
            continue
        if d <= nd: ins = i
        else: break
    SHEET.insert_row(row, ins+1, value_input_option="USER_ENTERED")
    return ins+1

# ─── NAVIGATION STACKS ------------------------------------------------------
def push_state(ctx, handler, args, title):
    st = ctx.user_data.setdefault("nav_stack", [])
    st.append({"handler": handler, "args": args, "title": title})
    # очищаем forward
    ctx.user_data["fwd_stack"] = []

def pop_state(ctx):
    nav = ctx.user_data.get("nav_stack", [])
    fwd = ctx.user_data.setdefault("fwd_stack", [])
    if nav:
        curr = nav.pop()  # убираем текущее
        if curr: fwd.append(curr)
    return nav.pop() if nav else None

def forward_state(ctx):
    fwd = ctx.user_data.get("fwd_stack", [])
    nav = ctx.user_data.setdefault("nav_stack", [])
    if fwd:
        nxt = fwd.pop()
        nav.append(nxt)
        return nxt
    return None

def breadcrumbs(ctx):
    nav = ctx.user_data.get("nav_stack", [])
    return " > ".join(item["title"] for item in nav)

# ─── KEYBOARDS --------------------------------------------------------------
def nav_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Назад", callback_data="back"),
        InlineKeyboardButton("🏠 Домой", callback_data="home"),
        InlineKeyboardButton("▶️ Вперёд", callback_data="forward")
    ]])

# ─── SAFE EDIT --------------------------------------------------------------
async def safe_edit(msg, text, kb=None):
    kb = kb or nav_kb()
    hd = ""
    # если есть хлебные крошки
    if msg._effective_user_data := msg._effective_user_data if hasattr(msg, "_effective_user_data") else None:
        pass
    try:
        return await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except:
        return await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)

# ─── MAIN MENU --------------------------------------------------------------
def main_kb():
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("💰 Текущий", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлый", callback_data="profit_prev")],
        [InlineKeyboardButton("📊 KPI тек", callback_data="kpi"),
         InlineKeyboardButton("📊 KPI пр", callback_data="kpi_prev")],
        [InlineKeyboardButton("➕ Добавить", callback_data="add_rec"),
         InlineKeyboardButton("💵 Зарплата", callback_data="add_sal")],
        [InlineKeyboardButton("📜 История ЗП", callback_data="hist")],
        [InlineKeyboardButton("🗄 Export CSV", callback_data="export_menu")]
    ])
    return kb

async def show_main(msg, ctx):
    push_state(ctx, show_main, (), "Главное")
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg, f"{crumbs}\n\n📊 Главное меню", main_kb())

# ─── YEAR MENU --------------------------------------------------------------
def year_kb(year: str):
    buttons = [InlineKeyboardButton(f"📅 {name}", callback_data=f"mon_{year}-{i+1:02d}")
               for i, name in enumerate(MONTH_FULL)]
    rows = [buttons[i:i+4] for i in range(0,12,4)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_year(msg, ctx, year):
    push_state(ctx, show_year, (year,), f"Год {year}")
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg, f"{crumbs}\n\n📆 {year}", year_kb(year))

# ─── MONTH & DAY HELPERS ---------------------------------------------------
def half(entries, first_half: bool):
    return [e for e in entries if (pdate(e["date"]).day <= 15) == first_half]

def default_half(code: str):
    y,m = map(int, code.split("-"))
    t = dt.date.today()
    return "old" if (t.year,t.month)==(y,m) and t.day<=15 else "new"

def crumbs_month(code, flag):
    y,m = code.split("-")
    part = "01–15" if flag=="old" else "16–31"
    return f"{MONTH_FULL[int(m)-1]} {y} ({part})"

def crumbs_day(code, date):
    y,m = code.split("-")
    return f"{date} {MONTH_FULL[int(m)-1]} {y}"

# ─── MONTH VIEW -------------------------------------------------------------
def month_kb(code, flag, days):
    togg = "old" if flag=="new" else "new"
    rows = [[InlineKeyboardButton("Первая" if flag=="new" else "Вторая",
        callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_month(msg, ctx, code, flag=None):
    flag = flag or default_half(code)
    ent = ctx.bot_data["entries"].get(code, [])
    tx = [e for e in ent if "amount" in e]
    part = half(sorted(tx, key=lambda e:pdate(e["date"])), flag=="old")
    days = sorted({e["date"] for e in part}, key=pdate)
    total = sum(e["amount"] for e in part)
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e['amount']}" for e in part)
    push_state(ctx, show_month, (code,flag), crumbs_month(code,flag))
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg,
        f"{crumbs}\n\n<b>{crumbs_month(code,flag)}</b>\n{body}\n\n<b>Итого:</b> {total}",
        month_kb(code, flag, days)
    )

# ─── DAY VIEW ---------------------------------------------------------------
def day_kb(code, date, lst):
    rows = []
    for e in lst:
        rows.append([
            InlineKeyboardButton(f"❌", callback_data=f"drow_{e['row_idx']}_{code}_{date}"),
            InlineKeyboardButton(f"✏️", callback_data=f"edit_{e['row_idx']}")
        ])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def show_day(msg, ctx, code, date):
    ent = ctx.bot_data["entries"].get(code, [])
    lst = [e for e in ent if e["date"]==date and "amount" in e]
    total = sum(e["amount"] for e in lst)
    body = "\n".join(f"{e['symbols']} · {e['amount']}" for e in lst) or "Записей нет"
    push_state(ctx, show_day, (code,date), crumbs_day(code,date))
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg,
        f"{crumbs}\n\n<b>{crumbs_day(code,date)}</b>\n{body}\n\n<b>Итого:</b> {total}",
        day_kb(code,date,lst)
    )

# ─── STATISTICS -------------------------------------------------------------
async def show_stat(msg, ctx, code, flag):
    ent = half(ctx.bot_data["entries"].get(code, []), flag=="old")
    if not ent:
        return await safe_edit(msg, "Нет данных", nav_kb())
    turn = sum(e.get("amount",0) for e in ent)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ent})
    avg  = round(sal/days,2) if days else 0
    push_state(ctx, show_stat, (code,flag), "Статистика")
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg,
        f"{crumbs}\n\n📊 Статистика\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}\n• Ср/день: {avg}"
    )

# ─── KPI --------------------------------------------------------------------
async def show_kpi(msg, ctx, prev=False):
    t = dt.date.today(); code = f"{t.year}-{t.month:02d}"
    flag = "old" if (prev or t.day<=15) else "new"
    ent = half(ctx.bot_data["entries"].get(code, []), flag=="old")
    if not ent:
        return await safe_edit(msg, "Нет данных за период", nav_kb())
    turn = sum(e.get("amount",0) for e in ent)
    sal  = round(turn*0.10,2)
    days = len({e["date"] for e in ent})
    plen=15
    avg  = round(sal/days,2) if days else 0
    push_state(ctx, show_kpi, (prev,), "KPI")
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg,
        f"{crumbs}\n\n📈 KPI\n• Оборот: {turn}\n• ЗП 10%: {sal}\n• Дней: {days}/{plen}\n• Ср/день: {avg}"
    )

# ─── HISTORY ----------------------------------------------------------------
async def show_history(msg, ctx):
    lst = [e for v in ctx.bot_data["entries"].values() for e in v if "salary" in e]
    if not lst:
        return await safe_edit(msg, "История пуста", nav_kb())
    lst.sort(key=lambda e:pdate(e["date"]))
    total = sum(e["salary"] for e in lst)
    body = "\n".join(f"{e['date']} · {e['salary']}" for e in lst)
    push_state(ctx, show_history, (), "История")
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg,
        f"{crumbs}\n\n📜 История ЗП\n{body}\n\n<b>Всего:</b> {total}"
    )

# ─── PROFIT -----------------------------------------------------------------
async def show_profit(msg, ctx, title, start, end):
    tot = sum(e.get("amount",0) for v in ctx.bot_data["entries"].values()
              for e in v if start<=pdate(e["date"])<=end)
    push_state(ctx, show_profit, (title,start,end), title)
    crumbs = breadcrumbs(ctx)
    return await safe_edit(msg, f"{crumbs}\n\n{title}\n• 10%: {round(tot*0.10,2)}")

# ─── EXPORT CSV -------------------------------------------------------------
async def cmd_export(update, ctx):
    if not ctx.args or not re.fullmatch(r"\d{4}-\d{2}", ctx.args[0]):
        return await update.message.reply_text("Использование: /export YYYY-MM")
    code = ctx.args[0]
    ent = ctx.bot_data["entries"].get(code, [])
    if not ent:
        return await update.message.reply_text("Нет данных за этот месяц")
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["Дата","Имя","Сумма"])
    for e in ent:
        v = e.get("amount") or e.get("salary")
        w.writerow([e["date"], e["symbols"], v])
    buf.seek(0)
    await update.message.reply_document(document=buf, filename=f"export_{code}.csv")

# ─── SEARCH ---------------------------------------------------------------
async def cmd_search(update, ctx):
    q = " ".join(ctx.args).strip()
    ent = [e for v in ctx.bot_data["entries"].values() for e in v]
    res = []
    # диапазон дат
    if m:=re.match(r"^(\d{2}\.\d{2}\.\d{4})-(\d{2}\.\d{2}\.\d{4})$", q):
        d1,d2 = map(pdate, m.groups())
        res = [e for e in ent if d1<=pdate(e["date"])<=d2]
    # сравнение суммы
    elif m:=re.match(r"^([<>])\s*(\d+)$", q):
        op,val = m.group(1), float(m.group(2))
        res = [e for e in ent if (e.get("amount") or e.get("salary") or 0) >
               val] if op==">" else [e for e in ent if (e.get("amount") or e.get("salary") or 0) <
               val]
    else:
        res = [e for e in ent if q.lower() in e["symbols"].lower()]
    if not res:
        return await update.message.reply_text("Ничего не найдено")
    res.sort(key=lambda e:pdate(e["date"]))
    body = "\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary',e.get('amount'))}" for e in res)
    await update.message.reply_text(body)

# ─── EDIT RECORD ------------------------------------------------------------
async def cmd_edit(upd, ctx):
    idx = int(upd.callback_query.data.split("_")[1])
    ctx.user_data["edit_row"] = idx
    await upd.callback_query.message.reply_text(
        "✏️ Введите новое имя и сумму через пробел, пример: Петя 1234"
    )

# ─── REMINDER ---------------------------------------------------------------
async def reminder(ctx: ContextTypes.DEFAULT_TYPE):
    for cid in ctx.application.bot_data.get("chats", set()):
        try:
            await ctx.bot.send_message(cid, "⏰ Не забудьте внести записи за сегодня!")
        except Exception as e:
            logging.warning(f"reminder error: {e}")

# ─── ADD FLOW & PASSWORD BINDING -------------------------------------------
async def ask_rec(update, ctx):
    # проверка пароля
    if not ctx.user_data.get("bound"):
        await update.message.reply_text("🔒 Введите пароль доступа:")
        ctx.user_data["await_pwd"] = True
        return
    target = ctx.match and ctx.match.group(1)
    if target:
        ad = {"step":"sym","date":target}
        prompt = await update.message.reply_text("✏️ Введите имя:")
        ad["prompt_msg"] = prompt
    else:
        ad = {"step":"date"}
        inline = InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today_sel")]])
        inline_msg = await update.message.reply_text(
            "📅 Укажите дату (ДД.MM.ГГГГ) или нажмите «Сегодня»:", reply_markup=inline
        )
        ad["inline_msg"] = inline_msg
    ctx.user_data["add"] = ad

async def ask_sal(update, ctx):
    if not ctx.user_data.get("bound"):
        await update.message.reply_text("🔒 Введите пароль доступа:")
        ctx.user_data["await_pwd"] = True
        return
    ad = {"step":"val","mode":"salary","date":sdate(dt.date.today())}
    prompt = await update.message.reply_text("💵 Введите сумму:")
    ad["prompt_msg"] = prompt
    ctx.user_data["add"] = ad

async def process_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = u.message.text.strip()
    # пароль
    if ctx.user_data.get("await_pwd"):
        if txt == "1750":
            ctx.user_data["bound"] = True
            ctx.user_data.pop("await_pwd",None)
            return await show_main(u.message, ctx)
        else:
            return await u.message.reply_text("❌ Неверный пароль")
    # редактирование
    if ctx.user_data.get("edit_row"):
        row = ctx.user_data.pop("edit_row")
        name,val = txt.split(maxsplit=1)
        if SHEET:
            SHEET.update_cell(row,2,name)
            SHEET.update_cell(row,3,val)
        return await u.message.reply_text(f"✅ Запись {row} обновлена")
    ad = ctx.user_data.get("add")
    if not ad:
        return
    # удаляем ответ пользователя
    try: await u.message.delete()
    except: pass

    step = ad["step"]
    if step=="date":
        if txt and not is_date(txt):
            return await u.message.reply_text("Формат ДД.MM.ГГГГ")
        ad["date"] = txt or sdate(dt.date.today()); ad["step"] = "sym"
        try: await ad["inline_msg"].delete()
        except: pass
        prompt = await u.message.reply_text("✏️ Введите имя:")
        ad["prompt_msg"] = prompt
        return

    if step=="sym":
        ad["symbols"] = txt; ad["step"] = "val"
        try: await ad["prompt_msg"].delete()
        except: pass
        prompt = await u.message.reply_text("💰 Введите сумму:")
        ad["prompt_msg"] = prompt
        return

    if step=="val":
        try: val = float(txt.replace(",",".")) 
        except: return await u.message.reply_text("Нужно число")
        if ad.get("mode")=="salary": ad["salary"] = val
        else:                         ad["amount"] = val

        row = push_row(ad); ctx.bot_data["entries"] = read_sheet()
        ctx.user_data.pop("add",None)
        try: await ad["prompt_msg"].delete()
        except: pass

        chat_id = u.effective_chat.id
        resp = await u.message.reply_html(
            f"✅ Запись добавлена:\n<b>{ad['symbols']}</b> — <b>{val}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↺ Отменить", callback_data=f"undo_{row}")]])
        )
        ctx.user_data["undo"] = {"row":row,"expires":dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)}
        ctx.application.job_queue.run_once(
            lambda jc: jc.bot.delete_message(chat_id, resp.message_id),
            when=UNDO_WINDOW
        )
        return

# ─── CALLBACK ROUTER --------------------------------------------------------
async def cb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = upd.callback_query
    if not q: return
    d, m = q.data, q.message
    await q.answer()

    if d=="back":
        prev = pop_state(ctx)
        if not prev:
            return await show_main(m,ctx)
        try: await m.delete()
        except: pass
        return await prev["handler"](m,ctx, *prev["args"])
    if d=="home":
        ctx.user_data["nav_stack"] = []
        ctx.user_data["fwd_stack"] = []
        return await show_main(m,ctx)
    if d=="forward":
        nxt = forward_state(ctx)
        if nxt:
            try: await m.delete()
            except: pass
            return await nxt["handler"](m,ctx,*nxt["args"])
        return

    if d.startswith("undo_"):
        row = int(d.split("_")[1])
        undo = ctx.user_data.get("undo")
        if not undo or undo["row"]!=row or dt.datetime.utcnow()>undo["expires"]:
            return await m.reply_text("Срок отмены вышел")
        delete_row(row); ctx.bot_data["entries"]=read_sheet()
        resp = await m.reply_text("🚫 Запись удалена")
        ctx.application.job_queue.run_once(
            lambda jc: jc.bot.delete_message(resp.chat_id, resp.message_id),
            when=UNDO_WINDOW
        )
        return

    if d.startswith("edit_"):
        return await cmd_edit(upd,ctx)
    if d=="today_sel":
        ad = ctx.user_data.get("add")
        if ad and ad["step"]=="date":
            ad["date"] = sdate(dt.date.today()); ad["step"]="sym"
            try: await ad["inline_msg"].delete()
            except: pass
            prompt = await m.reply_text("✏️ Введите имя:")
            ad["prompt_msg"]=prompt
        return
    if d=="go_today":
        t=dt.date.today(); mc,dd=f"{t.year}-{t.month:02d}",sdate(t)
        push_state(ctx, show_day, (mc,dd), crumbs_day(mc,dd))
        return await show_day(m,ctx,mc,dd)

    # прочие маршруты:
    if d.startswith("year_"):
        return await show_year(m,ctx,d.split("_")[1])
    if d.startswith("mon_"):
        _,code = d.split("_",1)
        return await show_month(m,ctx,*code.rsplit("-",1))
    if d.startswith("tgl_"):
        _,code,fl = d.split("_")
        return await show_month(m,ctx,code,fl)
    if d.startswith("day_"):
        _,code,dd = d.split("_")
        return await show_day(m,ctx,code,dd)
    if d=="stat":
        return await show_stat(m,ctx,*ctx.user_data["nav_stack"][-1]["args"])
    if d=="kpi":
        return await show_kpi(m,ctx,False)
    if d=="kpi_prev":
        return await show_kpi(m,ctx,True)
    if d=="profit_now":
        start,end = (dt.date.today().replace(day=1), dt.date.today())
        return await show_profit(m,ctx,"💰 Текущий",start,end)
    if d=="profit_prev":
        start = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).replace(day=16)
        end = start - dt.timedelta(days=1)
        return await show_profit(m,ctx,"💼 Прошлый",start,end)
    if d=="hist":
        return await show_history(m,ctx)
    if d=="export_menu":
        # показать клавиши экспорта?
        return

    if d.startswith("addmon_"):
        code = d.split("_",1)[1]
        return await ask_rec(update=Update.de_json({"message":{"text":""}},None), ctx=ctx)
    if d.startswith("addday_"):
        code,dd=d.split("_",2)[1:]
        return await ask_rec(update=Update.de_json({"match":({"group":lambda x:dd})},None), ctx=ctx)

# ─── START & RUN ------------------------------------------------------------
async def cmd_start(update, ctx):
    if not ctx.user_data.get("bound"):
        await update.message.reply_text("🔒 Введите пароль доступа:")
        ctx.user_data["await_pwd"] = True
    else:
        return await show_main(update.message, ctx)

if __name__=="__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.bot_data["entries"] = read_sheet()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_text))

    app.job_queue.run_repeating(auto_sync, interval=5, first=0)
    hh,mm = REMIND_HH_MM
    app.job_queue.run_daily(reminder, time=dt.time(hour=hh, minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()