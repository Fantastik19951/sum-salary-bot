import os, logging, datetime as dt, re
from collections import deque, defaultdict
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ─── CONFIG ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN")
DATE_FMT     = "%d.%m.%Y"
DATE_RX      = re.compile(r"\d{2}\.\d{2}\.\d{4}$")
HEADER_ROWS  = 4
UNDO_WINDOW  = 30          # сек. для «↺ Отмена»
REMIND_HH_MM = (20, 0)     # 20:00 напоминание
MONTH_FULL   = ('Январь Февраль Март Апрель Май Июнь '
                'Июль Август Сентябрь Октябрь Ноябрь Декабрь').split()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

# ─── GOOGLE SHEETS ----------------------------------------------------------
def connect_sheet():
    scope = ["https://spreadsheets.google.com/feeds",
             "https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive.file",
             "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope)
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
    v = (v or '').strip().replace(',', '.')
    if v in ('', '-', '—'): return None
    try: return float(v)
    except: return None

# ─── SHEET I/O --------------------------------------------------------------
def read_sheet():
    data = defaultdict(list)
    if not SHEET: return data
    for idx, row in enumerate(SHEET.get_all_values(), 1):
        if idx <= HEADER_ROWS or len(row) < 2: continue
        d = row[0].strip()
        if not is_date(d): continue
        e = {'date': d, 'symbols': row[1].strip(), 'row_idx': idx}
        amt = safe_float(row[2]) if len(row) > 2 else None
        sal = safe_float(row[3]) if len(row) > 3 else None
        if amt is None and sal is None: continue
        e['salary' if sal is not None else 'amount'] = sal or amt
        data[f"{pdate(d).year}-{pdate(d).month:02d}"].append(e)
    return data
async def auto_sync(ctx): ctx.application.bot_data["entries"] = read_sheet()
def delete_row(idx): SHEET and SHEET.delete_rows(idx)

def push_row(entry) -> int | None:
    if not SHEET: return None
    nd  = pdate(entry['date'])
    row = [entry['date'],
           entry.get('symbols', ''),
           entry.get('amount',  ''),
           entry.get('salary',  '')]

    col = SHEET.col_values(1)[HEADER_ROWS:]
    ins = HEADER_ROWS
    for i, v in enumerate(col, start=HEADER_ROWS + 1):
        try: d = pdate(v.strip())
        except: continue
        if d <= nd: ins = i
        elif d > nd: break
    SHEET.insert_row(row, ins + 1, value_input_option="USER_ENTERED")
    return ins + 1                  # индекс вставленной строки

# ─── UI & NAV ---------------------------------------------------------------
def nav_kb(): return InlineKeyboardMarkup(
    [[InlineKeyboardButton("⬅️ Назад", callback_data="back"),
      InlineKeyboardButton("🏠 Главное", callback_data="main")]])
async def safe_edit(msg, text, kb):
    try:    await msg.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except: await msg.reply_text(text, parse_mode='HTML', reply_markup=kb)

def nav_push(ctx, code): ctx.user_data.setdefault("nav", deque(maxlen=30)).append(code)
def nav_prev(ctx):
    st: deque = ctx.user_data.get("nav", deque())
    if st: st.pop()
    return st.pop() if st else "main"

# ─── MAIN MENU --------------------------------------------------------------
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 2024", callback_data="year_2024"),
         InlineKeyboardButton("📅 2025", callback_data="year_2025")],
        [InlineKeyboardButton("📆 Сегодня", callback_data="go_today")],
        [InlineKeyboardButton("💰 Текущий заработок", callback_data="profit_now"),
         InlineKeyboardButton("💼 Прошлый заработок", callback_data="profit_prev")],
        [InlineKeyboardButton("📊 KPI текущего",    callback_data="kpi"),
         InlineKeyboardButton("📊 KPI предыдущего", callback_data="kpi_prev")],
        [InlineKeyboardButton("➕ Запись",   callback_data="add_rec"),
         InlineKeyboardButton("💵 Зарплата", callback_data="add_sal")],
        [InlineKeyboardButton("📜 История зарплат", callback_data="hist")]
    ])
async def show_main(m): await safe_edit(m, "📊 Главное меню", main_kb())

# ─── YEAR MENU --------------------------------------------------------------
def year_kb(year:str):
    buttons=[InlineKeyboardButton(f"📅 {name}",callback_data=f"mon_{year}-{i+1:02d}")
             for i,name in enumerate(MONTH_FULL)]
    rows=[buttons[i:i+4] for i in range(0,12,4)]
    rows.extend(nav_kb().inline_keyboard)
    return InlineKeyboardMarkup(rows)
async def show_year(m,y): await safe_edit(m,f"📆 {y}",year_kb(y))

# ─── MONTH & DAY helpers ----------------------------------------------------
def half(entries, first_half: bool):
    return [e for e in entries if (pdate(e['date']).day <= 15) == first_half]
def default_half(code:str):
    y,m=map(int,code.split('-')); t=dt.date.today()
    return 'old' if (t.year,t.month)==(y,m) and t.day<=15 else 'new'
def crumbs_month(code,flag):
    y,m=code.split('-')
    return f"{y} · {MONTH_FULL[int(m)-1]} · {'01-15' if flag=='old' else '16-31'}"
def crumbs_day(code,date):
    y,m=code.split('-')
    return f"{y} · {MONTH_FULL[int(m)-1]} · {date}"

# ─── MONTH VIEW -------------------------------------------------------------
def month_kb(code,flag,days):
    togg='old' if flag=='new' else 'new'
    rows=[[InlineKeyboardButton("📂 "+("Первая" if flag=='new' else "Вторая"),
                                callback_data=f"tgl_{code}_{togg}")]]
    for d in days:
        rows.append([InlineKeyboardButton(d, callback_data=f"day_{code}_{d}")])
    rows.append([InlineKeyboardButton("📊 Статистика", callback_data=f"stat_{code}_{flag}")])
    rows.append([InlineKeyboardButton("➕ Запись (месяц)", callback_data=f"addmon_{code}")])
    rows.extend(nav_kb().inline_keyboard)
    return InlineKeyboardMarkup(rows)
async def show_month(m,ctx,code,flag=None):
    flag  = flag or default_half(code)
    part  = half(sorted(ctx.bot_data["entries"].get(code, []),
                        key=lambda e:pdate(e['date'])),
                 flag=='old')
    days  = sorted({e['date'] for e in part}, key=pdate)
    total = sum(e.get('salary',e.get('amount',0)) for e in part)
    body  = "\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary',e.get('amount'))}" for e in part)

    await safe_edit(m,
        f"<b>{crumbs_month(code,flag)}</b>\n{body}\n\n<b>Итого:</b> {total}",
        month_kb(code,flag,days))

# ─── DAY VIEW ---------------------------------------------------------------
def day_kb(code,date,lst):
    rows=[[InlineKeyboardButton(f"❌ {e['symbols']}",
                                callback_data=f"drow_{e['row_idx']}_{code}_{date}")]
          for e in lst]
    rows.append([InlineKeyboardButton("➕ Запись (день)",callback_data=f"addday_{code}_{date}")])
    rows.extend(nav_kb().inline_keyboard)
    return InlineKeyboardMarkup(rows)
async def show_day(m,ctx,code,date):
    lst=[e for e in ctx.bot_data["entries"].get(code,[]) if e['date']==date]
    total=sum(e.get('salary',e.get('amount',0)) for e in lst)
    body="\n".join(f"{e['symbols']} · {e.get('salary',e.get('amount'))}"
                   for e in lst) if lst else "Записей нет"

    await safe_edit(m,
        f"<b>{crumbs_day(code,date)}</b>\n{body}\n\n<b>Итого:</b> {total}",
        day_kb(code,date,lst))

# ─── STATISTICS -------------------------------------------------------------
async def show_stat(m,ctx,code,flag):
    entries=half(ctx.bot_data["entries"].get(code,[]), flag=='old')
    if not entries: return await safe_edit(m,"Нет данных",nav_kb())

    turn   = sum(e.get('amount',0) for e in entries)
    salary = round(turn*0.10,2)             # 10 % от оборота
    days   = len({e['date'] for e in entries})
    avg    = round(salary/days,2) if days else 0

    await safe_edit(m,
        f"<b>{crumbs_month(code,flag)} · статистика</b>\n"
        f"• Оборот: {turn}\n"
        f"• ЗП (10 %): {salary}\n"
        f"• Дней с данными: {days}\n"
        f"• Среднее/день: {avg}", nav_kb())

# ─── KPI --------------------------------------------------------------------
def current_half_code():
    t=dt.date.today()
    return f"{t.year}-{t.month:02d}", ('old' if t.day<=15 else 'new')
def previous_half_code():
    t=dt.date.today()
    if t.day<=15:
        prev=(t.replace(day=1)-dt.timedelta(days=1))
        return f"{prev.year}-{prev.month:02d}", 'new'
    return f"{t.year}-{t.month:02d}", 'old'

def calc_kpi(entries,flag,finished=False):
    first=(flag=='old')
    period_len=15 if first else (
        (dt.date(pdate(entries[0]['date']).year,
                 pdate(entries[0]['date']).month%12+1,1)
         - dt.date(pdate(entries[0]['date']).year,
                   pdate(entries[0]['date']).month,16)).days )
    turn=sum(e.get('amount',0) for e in entries)
    salary=round(turn*0.10,2)
    days = len({e['date'] for e in entries}) or 1
    avg  = salary/days
    fc   = salary if finished else round(avg*period_len,2)
    return turn,salary,days,period_len,avg,fc

async def show_kpi(m,ctx,prev=False):
    code,flag = previous_half_code() if prev else current_half_code()
    entries = half(ctx.bot_data["entries"].get(code,[]), flag=='old')
    if not entries:
        return await safe_edit(m,"Нет данных за период",nav_kb())

    turn,sal,days,plen,avg,fc = calc_kpi(entries,flag,finished=prev)
    await safe_edit(m,
        f"<b>KPI — {crumbs_month(code,flag)}</b>\n"
        f"• Оборот: {turn}\n"
        f"• ЗП 10 %: {sal}\n"
        f"• Дней с данными: {days}/{plen}\n"
        f"• Среднее/день: {round(avg,2)}\n"
        f"• Прогноз до конца периода: {fc}", nav_kb())

# ─── HISTORY ----------------------------------------------------------------
async def show_history(m,ctx):
    lst=[e for v in ctx.bot_data["entries"].values() for e in v if 'salary'in e]
    if not lst: return await safe_edit(m,"История пуста",nav_kb())
    lst.sort(key=lambda e:pdate(e['date']))
    total=sum(e['salary'] for e in lst)
    body="\n".join(f"{e['date']} · {e['salary']}" for e in lst)
    await safe_edit(m,f"<b>📜 История ЗП</b>\n{body}\n\n<b>Всего:</b> {total}",nav_kb())

# ─── QUICK PROFIT -----------------------------------------------------------
def bounds_today():
    d=dt.date.today()
    return (d.replace(day=1) if d.day<=15 else d.replace(day=16)), d
def bounds_prev():
    cs,_=bounds_today(); pe=cs-dt.timedelta(days=1)
    return (pe.replace(day=1) if pe.day<=15 else pe.replace(day=16)), pe
def sum_period(ent,s,e):
    return sum(x.get('amount',0) for v in ent.values() for x in v
               if s<=pdate(x['date'])<=e)
async def show_profit(m,ctx,s,e,title):
    tot=sum_period(ctx.bot_data["entries"],s,e)
    await safe_edit(m,f"{title}\n<b>10 %:</b> {round(tot*0.10,2)}",nav_kb())

# ─── ADD FLOW (ask_rec / ask_sal / process_text) ----------------------------
SUM_RKB=ReplyKeyboardMarkup([["100","500","1000"],["Очистить"]],
                            resize_keyboard=True,one_time_keyboard=True)

async def ask_rec(m,ctx,target=None,mon=None):
    ctx.user_data['add']={'step':'date','forced':target,'fixed':mon}
    if target:
        ctx.user_data['add']['date']=target; ctx.user_data['add']['step']='sym'
        return await m.reply_text("👤 Имя :",reply_markup=ReplyKeyboardRemove())
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Сегодня",callback_data="today_sel")]])
    await safe_edit(m,"📅 Дата :",kb)

async def ask_sal(m,ctx):
    ctx.user_data['add']={'mode':'salary','step':'val','date':sdate(dt.date.today())}
    await safe_edit(m,"💵 Сумма ЗП :",nav_kb())

async def process_text(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    ad=ctx.user_data.get('add')
    if not ad: return
    txt=u.message.text.strip()
    if txt=="Очистить":
        return await u.message.reply_text("Введите сумму :",reply_markup=SUM_RKB)

    # step date
    if ad['step']=='date':
        if txt and not is_date(txt):
            return await u.message.reply_text("Формат ДД.ММ.ГГГГ")
        ad['date']=txt or sdate(dt.date.today())
        if ad.get('fixed'):
            d=pdate(ad['date']); y,m=map(int,ad['fixed'].split('-'))
            ad['date']=sdate(dt.date(y,m,d.day))
        ad['step']='sym' if ad.get('mode')!='salary' else 'val'
        return await u.message.reply_text(
            "👤 Имя :" if ad['step']=='sym' else "💰 Сумма :",
            reply_markup=ReplyKeyboardRemove())

    # step symbols
    if ad['step']=='sym':
        ad['symbols']=txt; ad['step']='val'
        return await u.message.reply_text("💰 Сумма :",reply_markup=SUM_RKB)

    # step value
    try: val=float(txt.replace(',','.'))
    except ValueError:
        return await u.message.reply_text("Число?")
    if ad.get('mode')=='salary': ad['salary']=val
    else: ad['amount']=val

    row=push_row(ad)
    ctx.bot_data["entries"]=read_sheet()
    ctx.user_data.pop('add')

    ctx.user_data['undo']={'row':row,'expires':dt.datetime.utcnow()+dt.timedelta(seconds=UNDO_WINDOW)}
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("↺ Отмена",callback_data=f"undo_{row}")]])
    await u.message.reply_text("✅ Сохранено",reply_markup=ReplyKeyboardRemove())
    await u.message.reply_text("↺ Можно отменить в течение 30 сек.",reply_markup=kb)

# ─── SEARCH COMMAND ---------------------------------------------------------
async def cmd_search(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    query=" ".join(ctx.args).strip()
    if not query:
        return await u.message.reply_text("Использование: /search слово_или_сумма")

    ent=[e for v in ctx.bot_data["entries"].values() for e in v]
    if query.replace(',','.').isdigit():
        val=float(query.replace(',','.'))
        res=[e for e in ent if e.get('amount')==val or e.get('salary')==val]
    else:
        q=query.lower(); res=[e for e in ent if q in e['symbols'].lower()]

    if not res:
        return await u.message.reply_text("Ничего не найдено")

    res.sort(key=lambda e:pdate(e['date']))
    body="\n".join(f"{e['date']} · {e['symbols']} · {e.get('salary',e.get('amount'))}" for e in res)
    await u.message.reply_text(body)

# ─── REMINDER ---------------------------------------------------------------
async def reminder(job_ctx:ContextTypes.DEFAULT_TYPE):
    for cid in job_ctx.application.bot_data.get('chats',set()):
        try: await job_ctx.bot.send_message(cid,"⏰ Не забудьте внести записи за сегодня!")
        except Exception as e: logging.warning(f"reminder: {e}")

# ─── ROUTER -----------------------------------------------------------------
async def cb(upd:Update,ctx:ContextTypes.DEFAULT_TYPE):
    q=upd.callback_query
    if not q: return
    d=q.data; m=q.message
    await q.answer()

    # undo
    if d.startswith("undo_"):
        row=int(d.split('_')[1])
        undo=ctx.user_data.get('undo')
        if not undo or undo['row']!=row or dt.datetime.utcnow()>undo['expires']:
            return await m.reply_text("Срок отмены вышел")
        delete_row(row); ctx.bot_data["entries"]=read_sheet()
        ctx.user_data.pop('undo',None)
        return await m.reply_text("🚫 Запись удалена")

    # today selector in add flow
    if d=="today_sel":
        ad=ctx.user_data.get('add')
        if ad and ad['step']=='date':
            ad['date']=sdate(dt.date.today()); ad['step']='sym'
            return await m.reply_text("👤 Имя :",reply_markup=ReplyKeyboardRemove())

    # open today
    if d=="go_today":
        t=dt.date.today(); mc=f"{t.year}-{t.month:02d}"; dd=sdate(t)
        nav_push(ctx,f"day_{mc}_{dd}")
        return await show_day(m,ctx,mc,dd)

    code=d if d!="back" else nav_prev(ctx)
    if d not in ("back","go_today"): nav_push(ctx,code)

    # routes
    if code=="main":        return await show_main(m)
    if code=="kpi":         return await show_kpi(m,ctx)
    if code=="kpi_prev":    return await show_kpi(m,ctx,prev=True)
    if code.startswith("year_"):
        return await show_year(m,code.split('_')[1])
    if code.startswith("mon_"):
        return await show_month(m,ctx,code.split('_')[1])
    if code.startswith("tgl_"):
        _,mc,fl=code.split('_')
        return await show_month(m,ctx,mc,fl)
    if code.startswith("stat_"):
        _,mc,fl=code.split('_')
        return await show_stat(m,ctx,mc,fl)
    if code.startswith("day_"):
        _,mc,dd=code.split('_')
        return await show_day(m,ctx,mc,dd)

    if code=="add_rec":     return await ask_rec(m,ctx)
    if code=="add_sal":     return await ask_sal(m,ctx)
    if code.startswith("addmon_"):
        return await ask_rec(m,ctx,mon=code.split('_')[1])
    if code.startswith("addday_"):
        _,mc,dd=code.split('_')
        return await ask_rec(m,ctx,target=dd,mon=mc)

    if code=="hist":        return await show_history(m,ctx)

    if code=="profit_now":
        s,e=bounds_today(); return await show_profit(m,ctx,s,e,"💰 Текущий заработок")
    if code=="profit_prev":
        s,e=bounds_prev();  return await show_profit(m,ctx,s,e,"💼 Прошлый заработок")

    if code.startswith("drow_"):
        _,row,mc,dd=code.split('_')
        delete_row(int(row)); ctx.bot_data["entries"]=read_sheet()
        return await show_day(m,ctx,mc,dd)

# ─── START & RUN ------------------------------------------------------------
async def cmd_start(u:Update,c):
    nav_push(c,"main")
    c.application.bot_data.setdefault('chats',set()).add(u.effective_chat.id)
    await u.message.reply_text("📊 Главное меню",reply_markup=main_kb())

if __name__=="__main__":
    app=ApplicationBuilder().token(TOKEN).build()
    app.bot_data["entries"]=read_sheet()

    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("search",cmd_search))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,process_text))

    hh,mm=REMIND_HH_MM
    app.job_queue.run_repeating(auto_sync,5,0)
    app.job_queue.run_daily(reminder,time=dt.time(hour=hh,minute=mm))

    logging.info("🚀 Bot up")
    app.run_polling()
