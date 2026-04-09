import os, time, asyncio, logging
import aiosqlite
from telegram import (
    Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────
#  Конфиг из ENV (задаются в Railway)
# ─────────────────────────────────────────
BOT_TOKEN           = os.environ["BOT_TOKEN"]
ADMIN_IDS           = list(map(int, os.environ.get("ADMIN_IDS","").split(","))) if os.environ.get("ADMIN_IDS") else []
MINE_INTERVAL_HOURS = float(os.environ.get("MINE_INTERVAL_HOURS", "2"))
BASE_MINE_REWARD    = float(os.environ.get("BASE_MINE_REWARD", "100"))
REFERRAL_BONUS_INV  = float(os.environ.get("REFERRAL_BONUS_INV", "50"))
REFERRAL_BONUS_NEW  = float(os.environ.get("REFERRAL_BONUS_NEW", "25"))
COIN_SYMBOL         = os.environ.get("COIN_SYMBOL", "NACKL")
DB_FILE             = "miner_bot.db"

# ═══════════════════════════════════════
# БД
# ═══════════════════════════════════════
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                balance     REAL    DEFAULT 0,
                rate        REAL    DEFAULT 1.0,
                referrer_id INTEGER DEFAULT NULL,
                last_mine   REAL    DEFAULT 0,
                total_mines INTEGER DEFAULT 0,
                is_banned   INTEGER DEFAULT 0,
                joined_at   REAL    DEFAULT (strftime('%s','now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, amount REAL,
                type TEXT, note TEXT,
                ts REAL DEFAULT (strftime('%s','now'))
            )
        """)
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as c:
            return await c.fetchone()

async def _log_tx(db, uid, amount, type_, note):
    await db.execute("INSERT INTO transactions (user_id,amount,type,note) VALUES (?,?,?,?)", (uid, amount, type_, note))

async def register_user(uid, username, first_name, ref_id=None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id,username,first_name,referrer_id) VALUES (?,?,?,?)", (uid, username or "", first_name or "", ref_id))
        if ref_id:
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REFERRAL_BONUS_NEW, uid))
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REFERRAL_BONUS_INV, ref_id))
            await _log_tx(db, uid,    REFERRAL_BONUS_NEW, "referral", "Бонус новому")
            await _log_tx(db, ref_id, REFERRAL_BONUS_INV, "referral", f"Реферал {uid}")
        await db.commit()

async def do_mine(uid):
    user = await get_user(uid)
    if not user: return False, 0
    now = time.time()
    elapsed = now - user["last_mine"]
    interval = MINE_INTERVAL_HOURS * 3600
    if elapsed < interval: return False, interval - elapsed
    reward = round(BASE_MINE_REWARD * user["rate"], 2)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET balance=balance+?,last_mine=?,total_mines=total_mines+1 WHERE user_id=?", (reward, now, uid))
        await _log_tx(db, uid, reward, "mine", "Клейм")
        await db.commit()
    return True, reward

async def count_refs(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (uid,)) as c:
            r = await c.fetchone(); return r[0] if r else 0

async def get_top(limit=10):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id,first_name,balance FROM users ORDER BY balance DESC LIMIT ?", (limit,)) as c:
            return await c.fetchall()

async def get_stats():
    async with aiosqlite.connect(DB_FILE) as db:
        tu = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        au = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_banned=0")).fetchone())[0]
        tb = (await (await db.execute("SELECT SUM(balance) FROM users")).fetchone())[0] or 0
        tm = (await (await db.execute("SELECT SUM(amount) FROM transactions WHERE type='mine'")).fetchone())[0] or 0
    return tu, au, tb, tm

# ═══════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════
def is_admin(uid): return uid in ADMIN_IDS

def fmt_time(sec):
    return f"{int(sec//3600):02d}:{int((sec%3600)//60):02d}:{int(sec%60):02d}"

def main_kb(admin=False):
    rows = [["⛏️ Майнить","💰 Кошелёк"],["🚀 Буст","🏆 Топ"],["👥 Рефералы","ℹ️ Профиль"]]
    if admin: rows.append(["👑 Админ-панель"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def mine_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛏️ Клеймить", callback_data="mine_claim")]])

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика",      callback_data="adm_stats"),
         InlineKeyboardButton("👥 Пользователи",    callback_data="adm_users")],
        [InlineKeyboardButton("➕ Начислить баланс", callback_data="adm_addbal"),
         InlineKeyboardButton("⚡ Изменить рейт",   callback_data="adm_rate")],
        [InlineKeyboardButton("📢 Рассылка",         callback_data="adm_broadcast"),
         InlineKeyboardButton("🚫 Забанить",         callback_data="adm_ban")],
        [InlineKeyboardButton("✅ Разбанить",        callback_data="adm_unban"),
         InlineKeyboardButton("🗑️ Сбросить баланс", callback_data="adm_reset")],
    ])

def not_banned(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = await get_user(update.effective_user.id)
        if u and u["is_banned"]:
            await update.effective_message.reply_text("🚫 Вы заблокированы."); return
        return await fn(update, ctx)
    wrap.__name__ = fn.__name__; return wrap

def admin_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.effective_message.reply_text("⛔ Нет доступа."); return
        return await fn(update, ctx)
    wrap.__name__ = fn.__name__; return wrap

# ═══════════════════════════════════════
# Хендлеры — пользователь
# ═══════════════════════════════════════
@not_banned
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ref_id = None
    if ctx.args:
        try:
            ref_id = int(ctx.args[0])
            if ref_id == u.id: ref_id = None
        except: pass
    existing = await get_user(u.id)
    if not existing:
        await register_user(u.id, u.username, u.first_name, ref_id)
        bonus = f"\n🎁 Реф-бонус: +{REFERRAL_BONUS_NEW} {COIN_SYMBOL}" if ref_id else ""
        text = (f"👋 Добро пожаловать, <b>{u.first_name}</b>!\n\n"
                f"⛏️ <b>Acki Nacki Cloud Miner</b>\n"
                f"Майните <b>${COIN_SYMBOL}</b> каждые {int(MINE_INTERVAL_HOURS)} часа.{bonus}")
    else:
        text = f"👋 С возвращением, <b>{u.first_name}</b>!"
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_kb(is_admin(u.id)))

@not_banned
async def msg_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await get_user(u.id)
    if not user: await cmd_start(update, ctx); return
    elapsed = time.time() - user["last_mine"]
    interval = MINE_INTERVAL_HOURS * 3600
    if elapsed >= interval:
        reward = round(BASE_MINE_REWARD * user["rate"], 2)
        await update.message.reply_text(
            f"⛏️ <b>Майнинг</b>\n\n🪙 Доступно: <b>{reward} {COIN_SYMBOL}</b>\n⚡ Рейт: x{user['rate']}",
            parse_mode="HTML", reply_markup=mine_kb())
    else:
        left = interval - elapsed
        prog = int(elapsed / interval * 20)
        bar = "█"*prog + "░"*(20-prog)
        await update.message.reply_text(
            f"⛏️ <b>Майнинг</b>\n\n⏳ Следующий клейм: <b>{fmt_time(left)}</b>\n"
            f"[{bar}] {int(elapsed/interval*100)}%\n\n"
            f"💰 Баланс: <b>{user['balance']:.2f} {COIN_SYMBOL}</b>\n⚡ Рейт: x{user['rate']}",
            parse_mode="HTML")

@not_banned
async def msg_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if not user: await cmd_start(update, ctx); return
    await update.message.reply_text(
        f"💰 <b>Кошелёк</b>\n\n🪙 Баланс: <b>{user['balance']:.4f} {COIN_SYMBOL}</b>\n"
        f"⛏️ Клеймов: <b>{user['total_mines']}</b>\n⚡ Рейт: <b>x{user['rate']}</b>",
        parse_mode="HTML")

@not_banned
async def msg_boost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = await get_user(update.effective_user.id)
    if not user: await cmd_start(update, ctx); return
    refs = await count_refs(update.effective_user.id)
    await update.message.reply_text(
        f"🚀 <b>Буст</b>\n\n👥 Рефералов: <b>{refs}</b> → +{refs*5}% к рейту\n"
        f"⚡ Ваш рейт: <b>x{user['rate']}</b>\n\n"
        f"• Приглашайте друзей (+5% за каждого)\n• Реф-ссылка в разделе 👥 Рефералы",
        parse_mode="HTML")

@not_banned
async def msg_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    top = await get_top(10)
    me = update.effective_user.id
    medals = ["🥇","🥈","🥉"] + ["🏅"]*7
    lines = [f"{medals[i]} {r['first_name'] or 'User'}: <b>{r['balance']:.2f} {COIN_SYMBOL}</b>" + (" ← вы" if r["user_id"]==me else "") for i,r in enumerate(top)]
    await update.message.reply_text("🏆 <b>Топ игроков</b>\n\n" + ("\n".join(lines) if lines else "Пока пусто."), parse_mode="HTML")

@not_banned
async def msg_refs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    refs = await count_refs(uid)
    bot = await ctx.bot.get_me()
    link = f"https://t.me/{bot.username}?start={uid}"
    await update.message.reply_text(
        f"👥 <b>Рефералы</b>\n\nПриглашено: <b>{refs}</b>\n"
        f"Бонус: +{REFERRAL_BONUS_INV} вам / +{REFERRAL_BONUS_NEW} другу\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n<code>{link}</code>",
        parse_mode="HTML")

@not_banned
async def msg_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = await get_user(u.id)
    if not user: await cmd_start(update, ctx); return
    refs = await count_refs(u.id)
    elapsed = time.time() - user["last_mine"]
    interval = MINE_INTERVAL_HOURS * 3600
    mine_st = "✅ Готово!" if elapsed >= interval else f"⏳ {fmt_time(interval-elapsed)}"
    joined = time.strftime("%d.%m.%Y", time.localtime(user["joined_at"]))
    adm = "👑 Администратор\n" if is_admin(u.id) else ""
    await update.message.reply_text(
        f"ℹ️ <b>Профиль</b>\n\n{adm}👤 {u.first_name}\n🆔 <code>{u.id}</code>\n📅 С: {joined}\n\n"
        f"💰 <b>{user['balance']:.4f} {COIN_SYMBOL}</b>\n⚡ Рейт: x{user['rate']}\n"
        f"⛏️ Клеймов: {user['total_mines']}\n👥 Рефералов: {refs}\n🕐 Майнинг: {mine_st}",
        parse_mode="HTML")

async def cb_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ok, val = await do_mine(q.from_user.id)
    if ok:
        await q.edit_message_text(f"✅ <b>+{val} {COIN_SYMBOL}!</b>\nСледующий клейм через {int(MINE_INTERVAL_HOURS)} ч.", parse_mode="HTML")
    else:
        await q.edit_message_text(f"⏳ Подождите ещё {fmt_time(val)}")

# ═══════════════════════════════════════
# Хендлеры — администратор
# ═══════════════════════════════════════
@admin_only
async def msg_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())

async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Нет доступа.", show_alert=True); return
    await q.answer()
    d = q.data
    if d == "adm_stats":
        tu,au,tb,tm = await get_stats()
        await q.edit_message_text(
            f"📊 <b>Статистика</b>\n\n👤 Всего: <b>{tu}</b>\n✅ Активных: <b>{au}</b>\n"
            f"💰 Суммарный баланс: <b>{tb:.2f} {COIN_SYMBOL}</b>\n⛏️ Выдано: <b>{tm:.2f} {COIN_SYMBOL}</b>",
            parse_mode="HTML", reply_markup=admin_kb())
    elif d == "adm_users":
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 10") as c:
                rows = await c.fetchall()
        lines = [f"• {r['first_name'] or 'id'+str(r['user_id'])} | {r['balance']:.1f} | x{r['rate']}" + (" 🚫" if r["is_banned"] else "") for r in rows]
        await q.edit_message_text("👥 <b>Последние 10:</b>\n\n" + ("\n".join(lines) or "Нет."), parse_mode="HTML", reply_markup=admin_kb())
    elif d == "adm_addbal":
        await q.edit_message_text("➕ Отправьте: <code>user_id сумма</code>\nПример: <code>123456789 500</code>", parse_mode="HTML")
        ctx.user_data["adm_action"] = "addbal"
    elif d == "adm_rate":
        await q.edit_message_text("⚡ Отправьте: <code>user_id рейт</code>\nПример: <code>123456789 2.5</code>", parse_mode="HTML")
        ctx.user_data["adm_action"] = "rate"
    elif d == "adm_broadcast":
        await q.edit_message_text("📢 Отправьте текст рассылки:")
        ctx.user_data["adm_action"] = "broadcast"
    elif d == "adm_ban":
        await q.edit_message_text("🚫 Отправьте <code>user_id</code>:", parse_mode="HTML")
        ctx.user_data["adm_action"] = "ban"
    elif d == "adm_unban":
        await q.edit_message_text("✅ Отправьте <code>user_id</code>:", parse_mode="HTML")
        ctx.user_data["adm_action"] = "unban"
    elif d == "adm_reset":
        await q.edit_message_text("🗑️ Отправьте <code>user_id</code> или <code>ALL</code>:", parse_mode="HTML")
        ctx.user_data["adm_action"] = "reset"

async def msg_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    action = ctx.user_data.pop("adm_action", None)
    if not action: return
    text = update.message.text.strip()
    try:
        if action == "addbal":
            uid, amt = int(text.split()[0]), float(text.split()[1])
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amt, uid))
                await _log_tx(db, uid, amt, "admin_add", "От администратора")
                await db.commit()
            await update.message.reply_text(f"✅ +{amt} {COIN_SYMBOL} → <code>{uid}</code>", parse_mode="HTML", reply_markup=admin_kb())
        elif action == "rate":
            uid, rate = int(text.split()[0]), float(text.split()[1])
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("UPDATE users SET rate=? WHERE user_id=?", (rate, uid))
                await db.commit()
            await update.message.reply_text(f"✅ Рейт <code>{uid}</code> → x{rate}", parse_mode="HTML", reply_markup=admin_kb())
        elif action == "broadcast":
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as c:
                    uids = [r[0] async for r in c]
            ok = fail = 0
            for uid in uids:
                try:
                    await ctx.bot.send_message(uid, f"📢 <b>Сообщение:</b>\n\n{text}", parse_mode="HTML")
                    ok += 1
                except: fail += 1
            await update.message.reply_text(f"📢 Готово. Отправлено: {ok}, ошибок: {fail}", reply_markup=admin_kb())
        elif action == "ban":
            uid = int(text)
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
                await db.commit()
            await update.message.reply_text(f"🚫 <code>{uid}</code> заблокирован.", parse_mode="HTML", reply_markup=admin_kb())
        elif action == "unban":
            uid = int(text)
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (uid,))
                await db.commit()
            await update.message.reply_text(f"✅ <code>{uid}</code> разблокирован.", parse_mode="HTML", reply_markup=admin_kb())
        elif action == "reset":
            async with aiosqlite.connect(DB_FILE) as db:
                if text.upper() == "ALL":
                    await db.execute("UPDATE users SET balance=0")
                else:
                    await db.execute("UPDATE users SET balance=0 WHERE user_id=?", (int(text),))
                await db.commit()
            await update.message.reply_text("🗑️ Готово.", reply_markup=admin_kb())
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

# ═══════════════════════════════════════
# Запуск
# ═══════════════════════════════════════
def main():
    async def post_init(app):
        await init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.Regex(r"^⛏️ Майнить$"),     msg_mine))
    app.add_handler(MessageHandler(filters.Regex(r"^💰 Кошелёк$"),     msg_wallet))
    app.add_handler(MessageHandler(filters.Regex(r"^🚀 Буст$"),         msg_boost))
    app.add_handler(MessageHandler(filters.Regex(r"^🏆 Топ$"),          msg_top))
    app.add_handler(MessageHandler(filters.Regex(r"^👥 Рефералы$"),     msg_refs))
    app.add_handler(MessageHandler(filters.Regex(r"^ℹ️ Профиль$"),     msg_profile))
    app.add_handler(MessageHandler(filters.Regex(r"^👑 Админ-панель$"), msg_admin_panel))
    app.add_handler(CallbackQueryHandler(cb_mine,  pattern="^mine_claim$"))
    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^adm_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_admin_input))
    print("✅ Бот запущен (polling)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
