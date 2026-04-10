"""
Acki Nacki Cloud Miner — полная копия механики
- Реальный 2-часовой таймер фарминга
- Автоклейм в фоне каждые 5 минут
- Буст через рефералов и ноды
- Задания, лидерборд
- Полный admin-панель
"""

import os, time, asyncio, logging
import aiosqlite
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)

# ══════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_IDS    = [int(x) for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip().isdigit()]
DB           = "nackl.db"
FARM_HOURS   = 2          # часов до следующего клейма
BASE_REWARD  = 100        # базовая награда
REF_GIVER    = 50         # бонус пригласившему
REF_TAKER    = 25         # бонус приглашённому
COIN         = "NACKL"

TASKS = [
    {"id":1, "name":"Join Telegram channel",  "reward":500,  "link":"https://t.me/ackinacki"},
    {"id":2, "name":"Follow on X (Twitter)",  "reward":500,  "link":"https://x.com/ackinacki"},
    {"id":3, "name":"Join Discord",           "reward":300,  "link":"https://discord.gg/ackinacki"},
    {"id":4, "name":"Invite 1 friend",        "reward":200,  "link":None, "req_refs":1},
    {"id":5, "name":"Invite 5 friends",       "reward":1000, "link":None, "req_refs":5},
    {"id":6, "name":"Invite 10 friends",      "reward":3000, "link":None, "req_refs":10},
    {"id":7, "name":"Mine 7 days in a row",   "reward":1500, "link":None, "req_streak":7},
    {"id":8, "name":"Reach 5,000 NACKL",      "reward":500,  "link":None, "req_balance":5000},
]

NODES = [
    {"id":1, "name":"Lite Node",   "price":2000,  "boost":0.5,  "desc":"Basic network node"},
    {"id":2, "name":"Full Node",   "price":8000,  "boost":2.0,  "desc":"Full node, higher rate"},
    {"id":3, "name":"Master Node", "price":25000, "boost":6.0,  "desc":"Maximum yield node"},
]

# ══════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                uid         INTEGER PRIMARY KEY,
                name        TEXT    DEFAULT '',
                username    TEXT    DEFAULT '',
                balance     REAL    DEFAULT 0,
                boost       REAL    DEFAULT 1.0,
                ref_by      INTEGER DEFAULT NULL,
                last_farm   REAL    DEFAULT 0,
                farm_end    REAL    DEFAULT 0,
                streak      INTEGER DEFAULT 0,
                last_streak REAL    DEFAULT 0,
                node        INTEGER DEFAULT 0,
                banned      INTEGER DEFAULT 0,
                joined      REAL    DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS done_tasks (
                uid INTEGER, tid INTEGER,
                PRIMARY KEY(uid, tid)
            );
            CREATE TABLE IF NOT EXISTS txlog (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                uid     INTEGER,
                amount  REAL,
                kind    TEXT,
                note    TEXT,
                ts      REAL DEFAULT (strftime('%s','now'))
            );
        """)
        await db.commit()

async def user_get(uid):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE uid=?", (uid,)) as c:
            return await c.fetchone()

async def user_reg(uid, name, username, ref_by=None):
    existing = await user_get(uid)
    if existing:
        # Update name/username if changed
        if existing["name"] != name or existing["username"] != username:
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE users SET name=?,username=? WHERE uid=?", (name, username, uid))
                await db.commit()
        return
    async with aiosqlite.connect(DB) as db:
        # Старт первого фарминга сразу
        now = time.time()
        farm_end = now + FARM_HOURS * 3600
        await db.execute(
            "INSERT OR IGNORE INTO users(uid,name,username,ref_by,last_farm,farm_end) VALUES(?,?,?,?,?,?)",
            (uid, name, username, ref_by, now, farm_end)
        )
        if ref_by:
            await db.execute("UPDATE users SET balance=balance+?,boost=boost+0.05 WHERE uid=?", (REF_GIVER, ref_by))
            await db.execute("UPDATE users SET balance=balance+? WHERE uid=?", (REF_TAKER, uid))
            await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,?,'ref','Referral bonus')", (ref_by, REF_GIVER))
            await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,?,'ref','Welcome bonus')", (uid, REF_TAKER))
        await db.commit()

async def user_refs(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (uid,)) as c:
            r = await c.fetchone(); return r[0] if r else 0

async def tasks_done(uid):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT tid FROM done_tasks WHERE uid=?", (uid,)) as c:
            return {r[0] async for r in c}

async def task_complete(uid, tid, reward):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO done_tasks(uid,tid) VALUES(?,?)", (uid, tid))
        await db.execute("UPDATE users SET balance=balance+? WHERE uid=?", (reward, uid))
        await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,?,'task','Task reward')", (uid, reward))
        await db.commit()

async def farm_claim(uid):
    """Claim farming reward. Returns (success, reward_amount)"""
    u = await user_get(uid)
    if not u: return False, 0
    now = time.time()
    if now < u["farm_end"]: return False, u["farm_end"] - now  # returns seconds left
    # Streak logic
    day = 86400
    new_streak = u["streak"] + 1 if (now - u["last_streak"]) < day * 1.5 else 1
    streak_bonus = min(new_streak * 0.05, 0.5)  # up to +50%
    reward = round(BASE_REWARD * u["boost"] * (1 + streak_bonus), 2)
    new_farm_end = now + FARM_HOURS * 3600
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE users SET balance=balance+?,last_farm=?,farm_end=?,streak=?,last_streak=? WHERE uid=?",
            (reward, now, new_farm_end, new_streak, now, uid)
        )
        await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,?,'farm','Mining reward')", (uid, reward))
        await db.commit()
    return True, reward

async def node_buy(uid, node_id, price, boost_add):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET balance=balance-?,boost=boost+?,node=? WHERE uid=?",
                         (price, boost_add, node_id, uid))
        await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,-?,'node','Node purchase')", (uid, price))
        await db.commit()

async def leaderboard():
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT uid,name,balance,boost,streak FROM users WHERE banned=0 ORDER BY balance DESC LIMIT 10"
        ) as c:
            return await c.fetchall()

async def all_users():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT uid FROM users WHERE banned=0") as c:
            return [r[0] async for r in c]

async def stats():
    async with aiosqlite.connect(DB) as db:
        tu  = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        act = (await (await db.execute("SELECT COUNT(*) FROM users WHERE banned=0")).fetchone())[0]
        tb  = (await (await db.execute("SELECT SUM(balance) FROM users")).fetchone())[0] or 0
        tm  = (await (await db.execute("SELECT SUM(amount) FROM txlog WHERE kind='farm'")).fetchone())[0] or 0
    return tu, act, tb, tm

# ══════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════
def is_admin(uid): return uid in ADMIN_IDS

def hms(sec):
    h,m,s = int(sec//3600), int((sec%3600)//60), int(sec%60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def pbar(done, total, n=18):
    if total <= 0: return "░"*n
    f = min(int(done/total*n), n)
    return "█"*f + "░"*(n-f)

def node_of(uid_node):
    return next((nd for nd in NODES if nd["id"] == uid_node), None)

# ══════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛏️ Mine",     callback_data="s_mine"),
         InlineKeyboardButton("🚀 Boost",    callback_data="s_boost")],
        [InlineKeyboardButton("💰 Wallet",   callback_data="s_wallet"),
         InlineKeyboardButton("🐋 Whale",    callback_data="s_whale")],
        [InlineKeyboardButton("📋 Tasks",    callback_data="s_tasks"),
         InlineKeyboardButton("👥 Friends",  callback_data="s_friends")],
        [InlineKeyboardButton("🏆 Top",      callback_data="s_top"),
         InlineKeyboardButton("🔄 Refresh",  callback_data="s_main")],
    ])

def kb_back(): return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="s_main")]])

def kb_mine_ready():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛏️  Claim Reward", callback_data="mine_do")],
        [InlineKeyboardButton("« Back",           callback_data="s_main")],
    ])

def kb_mine_wait():
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="s_main")]])

def kb_tasks(done_ids, refs, u_balance, u_streak):
    rows = []
    for t in TASKS:
        tid      = t["id"]
        name     = t["name"]
        reward   = t["reward"]
        complete = tid in done_ids
        # Check eligibility
        eligible = True
        if t.get("req_refs")    and refs        < t["req_refs"]:    eligible = False
        if t.get("req_streak")  and u_streak    < t["req_streak"]:  eligible = False
        if t.get("req_balance") and u_balance   < t["req_balance"]: eligible = False

        if complete:
            rows.append([InlineKeyboardButton(f"✅  {name}", callback_data="noop")])
        elif not eligible:
            rows.append([InlineKeyboardButton(f"🔒  {name}  (+{reward})", callback_data="noop")])
        else:
            link = t.get("link")
            if link:
                rows.append([
                    InlineKeyboardButton(f"🔗 {name}", url=link),
                    InlineKeyboardButton(f"✔ Claim +{reward}", callback_data=f"task_{tid}"),
                ])
            else:
                rows.append([InlineKeyboardButton(f"▶️  {name}  (+{reward})", callback_data=f"task_{tid}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="s_main")])
    return InlineKeyboardMarkup(rows)

def kb_whale(user_node):
    rows = []
    for nd in NODES:
        owned = nd["id"] <= user_node
        label = f"✅  {nd['name']}" if owned else f"🖥  {nd['name']}  —  {nd['price']:,} {COIN}"
        cb    = "noop" if owned else f"node_buy_{nd['id']}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton("« Back", callback_data="s_main")])
    return InlineKeyboardMarkup(rows)

def kb_node_confirm(node_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data=f"node_ok_{node_id}"),
         InlineKeyboardButton("❌ Cancel",  callback_data="s_whale")],
    ])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats",       callback_data="adm_stats"),
         InlineKeyboardButton("👥 Users",       callback_data="adm_users")],
        [InlineKeyboardButton("➕ Add balance",  callback_data="adm_addbal"),
         InlineKeyboardButton("⚡ Set boost",    callback_data="adm_setboost")],
        [InlineKeyboardButton("📢 Broadcast",   callback_data="adm_broadcast"),
         InlineKeyboardButton("🚫 Ban",         callback_data="adm_ban")],
        [InlineKeyboardButton("✅ Unban",       callback_data="adm_unban"),
         InlineKeyboardButton("🗑 Reset bal",   callback_data="adm_reset")],
    ])

# ══════════════════════════════════════════════════
# SCREEN TEXTS
# ══════════════════════════════════════════════════
def txt_main(u, refs):
    nd   = node_of(u["node"])
    node_str = f"🖥 {nd['name']}" if nd else "🖥 No node"
    return (
        f"🪨  <b>Acki Nacki Cloud Miner</b>\n\n"
        f"👤  {u['name']}\n"
        f"⭐  Balance: <b>{u['balance']:,.2f} {COIN}</b>\n"
        f"⚡  Boost: <b>x{u['boost']:.2f}</b>   🔥 Streak: <b>{u['streak']}d</b>\n"
        f"👥  Friends: <b>{refs}</b>   {node_str}"
    )

def txt_mine(u):
    now   = time.time()
    total = FARM_HOURS * 3600
    left  = u["farm_end"] - now
    ready = left <= 0

    # next_streak = what user will get on claim (accurate preview)
    next_streak  = u["streak"] + 1 if (now - u["last_streak"]) < 86400 * 1.5 else 1
    streak_bonus = min(next_streak * 5, 50)
    reward = round(BASE_REWARD * u["boost"] * (1 + streak_bonus / 100), 2)

    if ready:
        return (
            f"⛏️  <b>Mine</b>\n\n"
            f"✅  <b>Ready to claim!</b>\n\n"
            f"🪙  Reward: <b>{reward:,.2f} {COIN}</b>\n"
            f"⚡  Boost: x{u['boost']:.2f}\n"
            f"🔥  Streak day {next_streak}  (+{streak_bonus}% bonus)\n\n"
            f"Tap <b>Claim Reward</b> ⬇️"
        ), True
    else:
        elapsed = total - left
        bar = pbar(elapsed, total)
        pct = int(elapsed / total * 100)
        return (
            f"⛏️  <b>Mine</b>\n\n"
            f"⏳  Next claim in: <b>{hms(left)}</b>\n\n"
            f"[{bar}]  {pct}%\n\n"
            f"🪙  Reward: <b>{reward:,.2f} {COIN}</b>\n"
            f"⚡  Boost: x{u['boost']:.2f}\n"
            f"🔥  Streak: {u['streak']}d  →  day {next_streak} (+{streak_bonus}%)"
        ), False

def txt_boost(u, refs):
    ref_boost  = refs * 0.05
    nd         = node_of(u["node"])
    node_boost = nd["boost"] if nd else 0.0
    node_name  = nd["name"]  if nd else "None"
    streak_b   = min(u["streak"] * 5, 50)
    return (
        f"🚀  <b>Boost</b>\n\n"
        f"⚡  Current boost: <b>x{u['boost']:.2f}</b>\n\n"
        f"📊  <b>Breakdown:</b>\n"
        f"  • Base:             x1.00\n"
        f"  • Friends ({refs}):    +{ref_boost:.2f}\n"
        f"  • Node ({node_name}): +{node_boost:.2f}\n\n"
        f"🔥  <b>Daily streak: {u['streak']} days</b>\n"
        f"  • Streak bonus: <b>+{streak_b}%</b> per claim\n\n"
        f"<b>How to boost more:</b>\n"
        f"  👥 Invite friends  (+0.05 per friend)\n"
        f"  🐋 Buy a Whale node\n"
        f"  📋 Complete tasks\n"
        f"  ⛏️ Mine every day (streak)"
    )

async def txt_wallet2(u):
    """wallet with real tx count"""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT amount,kind,note,ts FROM txlog WHERE uid=? ORDER BY ts DESC LIMIT 6", (u["uid"],)
        ) as c:
            txs = await c.fetchall()
        async with db.execute(
            "SELECT COUNT(*) FROM txlog WHERE uid=? AND kind='farm'", (u["uid"],)
        ) as c:
            mines = (await c.fetchone())[0]
    lines = []
    for t in txs:
        sign = "+" if t["amount"] > 0 else ""
        d    = time.strftime("%d.%m %H:%M", time.localtime(t["ts"]))
        lines.append(f"  {sign}{t['amount']:.1f}  {t['note']}  <i>({d})</i>")
    hist = "\n".join(lines) if lines else "  No transactions yet"
    nd   = node_of(u["node"])
    return (
        f"💰  <b>Wallet</b>\n\n"
        f"⭐  Balance: <b>{u['balance']:,.4f} {COIN}</b>\n"
        f"⚡  Boost: x{u['boost']:.2f}\n"
        f"⛏️  Total claims: <b>{mines}</b>\n"
        f"🖥  Node: <b>{nd['name'] if nd else 'None'}</b>\n\n"
        f"📜  <b>Recent transactions:</b>\n{hist}"
    )

def txt_whale(u):
    nd  = node_of(u["node"])
    cur = f"🖥  Active: <b>{nd['name']}</b>  (+{nd['boost']} boost)" if nd else "🖥  Node: <b>None</b>"
    lines = []
    for n in NODES:
        owned = n["id"] <= u["node"]
        mark  = "✅ " if owned else ""
        lines.append(
            f"{mark}<b>{n['name']}</b>  —  {n['price']:,} {COIN}\n"
            f"  ⚡ +{n['boost']} boost  ·  {n['desc']}"
        )
    return (
        f"🐋  <b>Whale</b>\n\n"
        f"{cur}\n\n"
        f"🏗  <b>Available nodes:</b>\n\n" +
        "\n\n".join(lines) +
        f"\n\n💡 Nodes permanently increase your mining boost."
    )

def txt_tasks_header(done_count):
    return (
        f"📋  <b>Tasks</b>\n\n"
        f"Completed: <b>{done_count}/{len(TASKS)}</b>\n\n"
        f"Complete tasks to earn bonus {COIN}!"
    )

async def txt_friends(uid, bot_username):
    refs  = await user_refs(uid)
    link  = f"https://t.me/{bot_username}?start={uid}"
    earn  = refs * REF_GIVER
    return (
        f"👥  <b>Friends</b>\n\n"
        f"Invited: <b>{refs}</b>\n"
        f"Earned from refs: <b>{earn} {COIN}</b>\n\n"
        f"📢  <b>Bonuses:</b>\n"
        f"  You get:    +{REF_GIVER} {COIN} per friend\n"
        f"  They get:   +{REF_TAKER} {COIN} welcome bonus\n"
        f"  Boost:      +0.05 per friend\n\n"
        f"🔗  <b>Your invite link:</b>\n"
        f"<code>{link}</code>"
    )

async def txt_top(my_uid):
    top    = await leaderboard()
    medals = ["🥇","🥈","🥉"] + ["🏅"]*7
    lines  = []
    for i, r in enumerate(top):
        you  = "  ← you" if r["uid"] == my_uid else ""
        lines.append(
            f"{medals[i]}  {r['name'] or 'User'}  —  "
            f"<b>{r['balance']:,.0f}</b>  x{r['boost']:.1f}  🔥{r['streak']}d{you}"
        )
    return "🏆  <b>Leaderboard</b>\n\n" + ("\n".join(lines) if lines else "No players yet.")

# ══════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    ref = None
    if ctx.args:
        try:
            r = int(ctx.args[0])
            ref = r if r != u.id else None
        except: pass

    await user_reg(u.id, u.first_name or "User", u.username or "", ref)
    usr  = await user_get(u.id)
    refs = await user_refs(u.id)

    bonus = f"\n🎁  Referral bonus: <b>+{REF_TAKER} {COIN}</b>!" if ref else ""
    await update.message.reply_text(
        f"👋  Welcome, <b>{u.first_name}</b>!{bonus}\n\n"
        f"⛏️  Mine <b>${COIN}</b> every {FARM_HOURS} hours\n"
        f"🚀  Boost rate with friends & nodes\n"
        f"📋  Complete tasks for bonus rewards\n"
        f"🐋  Buy Whale nodes for permanent boosts\n\n"
        f"{txt_main(usr, refs)}",
        parse_mode="HTML",
        reply_markup=kb_main()
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    d   = q.data

    # ensure registered
    u = update.effective_user
    await user_reg(uid, u.first_name or "User", u.username or "", None)
    usr = await user_get(uid)

    if not usr or usr["banned"]:
        await q.edit_message_text("🚫 You are banned."); return

    # ── Main ──
    if d == "s_main":
        refs = await user_refs(uid)
        await q.edit_message_text(txt_main(usr, refs), parse_mode="HTML", reply_markup=kb_main())

    # ── Mine ──
    elif d == "s_mine":
        text, ready = txt_mine(usr)
        await q.edit_message_text(text, parse_mode="HTML",
            reply_markup=kb_mine_ready() if ready else kb_mine_wait())

    elif d == "mine_do":
        ok, val = await farm_claim(uid)
        if ok:
            usr2 = await user_get(uid)
            streak_b = min(usr2["streak"] * 5, 50)
            await q.edit_message_text(
                f"✅  <b>+{val:,.2f} {COIN} claimed!</b>\n\n"
                f"🔥  Streak: <b>{usr2['streak']} days</b>\n"
                f"📈  Streak bonus: +{streak_b}%\n"
                f"⭐  Balance: <b>{usr2['balance']:,.2f} {COIN}</b>\n\n"
                f"⏳  Next claim in <b>{FARM_HOURS} hours</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="s_mine")]]))
        else:
            # val = seconds left
            await q.edit_message_text(
                f"⏳  Not ready yet!\nCome back in <b>{hms(val)}</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="s_mine")]]))

    # ── Boost ──
    elif d == "s_boost":
        refs = await user_refs(uid)
        await q.edit_message_text(txt_boost(usr, refs), parse_mode="HTML", reply_markup=kb_back())

    # ── Wallet ──
    elif d == "s_wallet":
        text = await txt_wallet2(usr)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_back())

    # ── Whale ──
    elif d == "s_whale":
        await q.edit_message_text(txt_whale(usr), parse_mode="HTML",
            reply_markup=kb_whale(usr["node"]))

    elif d.startswith("node_buy_"):
        nid  = int(d.split("_")[2])
        nd   = next((n for n in NODES if n["id"] == nid), None)
        if not nd: return
        if usr["balance"] < nd["price"]:
            await q.answer(f"❌ Not enough {COIN}!", show_alert=True); return
        if usr["node"] >= nid:
            await q.answer("Already owned!", show_alert=True); return
        await q.edit_message_text(
            f"🐋  <b>Confirm purchase</b>\n\n"
            f"Node: <b>{nd['name']}</b>\n"
            f"Cost: <b>{nd['price']:,} {COIN}</b>\n"
            f"Boost: <b>+{nd['boost']}</b>\n\n"
            f"Your balance: {usr['balance']:,.2f} {COIN}",
            parse_mode="HTML",
            reply_markup=kb_node_confirm(nid)
        )

    elif d.startswith("node_ok_"):
        nid = int(d.split("_")[2])
        nd  = next((n for n in NODES if n["id"] == nid), None)
        if not nd or usr["balance"] < nd["price"]: return
        await node_buy(uid, nid, nd["price"], nd["boost"])
        usr2 = await user_get(uid)
        await q.edit_message_text(
            f"✅  <b>{nd['name']} activated!</b>\n\n"
            f"⚡  New boost: <b>x{usr2['boost']:.2f}</b>\n"
            f"⭐  Balance: {usr2['balance']:,.2f} {COIN}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="s_whale")]]))

    # ── Tasks ──
    elif d == "s_tasks":
        done = await tasks_done(uid)
        refs = await user_refs(uid)
        await q.edit_message_text(
            txt_tasks_header(len(done)),
            parse_mode="HTML",
            reply_markup=kb_tasks(done, refs, usr["balance"], usr["streak"])
        )

    elif d.startswith("task_"):
        tid  = int(d.split("_")[1])
        task = next((t for t in TASKS if t["id"] == tid), None)
        if not task: return
        done = await tasks_done(uid)
        if tid in done:
            await q.answer("Already completed ✅", show_alert=True); return
        refs = await user_refs(uid)
        # Check requirements
        if task.get("req_refs")    and refs           < task["req_refs"]:
            await q.answer(f"Need {task['req_refs']} friends (you have {refs})", show_alert=True); return
        if task.get("req_streak")  and usr["streak"]  < task["req_streak"]:
            await q.answer(f"Need {task['req_streak']}-day streak (you have {usr['streak']})", show_alert=True); return
        if task.get("req_balance") and usr["balance"] < task["req_balance"]:
            await q.answer(f"Need {task['req_balance']} {COIN}", show_alert=True); return
        await task_complete(uid, tid, task["reward"])
        await q.answer(f"✅  +{task['reward']} {COIN}!", show_alert=True)
        # Refresh tasks screen
        usr  = await user_get(uid)
        done = await tasks_done(uid)
        refs = await user_refs(uid)
        await q.edit_message_text(
            txt_tasks_header(len(done)), parse_mode="HTML",
            reply_markup=kb_tasks(done, refs, usr["balance"], usr["streak"])
        )

    elif d == "noop":
        await q.answer()

    # ── Friends ──
    elif d == "s_friends":
        me   = await ctx.bot.get_me()
        text = await txt_friends(uid, me.username)
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Share link", callback_data="friends_share")],
                [InlineKeyboardButton("« Back",        callback_data="s_main")],
            ])
        )

    elif d == "friends_share":
        me   = await ctx.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        await q.answer(f"Your link:\n{link}", show_alert=True)

    # ── Top ──
    elif d == "s_top":
        text = await txt_top(uid)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_back())

    # ── Admin ──
    elif d.startswith("adm_"):
        if not is_admin(uid):
            await q.answer("⛔ No access", show_alert=True); return
        await adm_cb(q, ctx, d)


# ══════════════════════════════════════════════════
# ADMIN
# ══════════════════════════════════════════════════
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ No access."); return
    await update.message.reply_text("👑  <b>Admin Panel</b>", parse_mode="HTML", reply_markup=kb_admin())

async def adm_cb(q, ctx, d):
    if d == "adm_stats":
        tu,act,tb,tm = await stats()
        await q.edit_message_text(
            f"📊  <b>Stats</b>\n\n"
            f"Total users: <b>{tu}</b>  (active: {act})\n"
            f"Total balance: <b>{tb:,.0f} {COIN}</b>\n"
            f"Total mined: <b>{tm:,.0f} {COIN}</b>",
            parse_mode="HTML", reply_markup=kb_admin()
        )
    elif d == "adm_users":
        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users ORDER BY joined DESC LIMIT 10") as c:
                rows = await c.fetchall()
        lines = [
            f"• {r['name']} ({r['uid']}) | {r['balance']:.0f} | x{r['boost']:.1f}" +
            (" 🚫" if r["banned"] else "")
            for r in rows
        ]
        await q.edit_message_text(
            "👥  <b>Last 10 users:</b>\n\n" + "\n".join(lines),
            parse_mode="HTML", reply_markup=kb_admin()
        )
    elif d in ("adm_addbal","adm_setboost","adm_broadcast","adm_ban","adm_unban","adm_reset"):
        prompts = {
            "adm_addbal":   "➕ Send: <code>uid amount</code>\nEx: <code>123456789 1000</code>",
            "adm_setboost": "⚡ Send: <code>uid boost</code>\nEx: <code>123456789 3.5</code>",
            "adm_broadcast":"📢 Send broadcast text:",
            "adm_ban":      "🚫 Send <code>uid</code> to ban:",
            "adm_unban":    "✅ Send <code>uid</code> to unban:",
            "adm_reset":    "🗑 Send <code>uid</code> or <code>ALL</code> to reset balance:",
        }
        await q.edit_message_text(prompts[d], parse_mode="HTML")
        ctx.user_data["adm"] = d.replace("adm_", "")

async def msg_adm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    action = ctx.user_data.pop("adm", None)
    if not action: return
    text = update.message.text.strip()
    try:
        async with aiosqlite.connect(DB) as db:
            if action == "addbal":
                uid, amt = int(text.split()[0]), float(text.split()[1])
                await db.execute("UPDATE users SET balance=balance+? WHERE uid=?", (amt, uid))
                await db.execute("INSERT INTO txlog(uid,amount,kind,note) VALUES(?,?,'admin','Admin add')", (uid, amt))
                await db.commit()
                await update.message.reply_text(f"✅  +{amt} {COIN} → {uid}", reply_markup=kb_admin())
            elif action == "setboost":
                uid, boost = int(text.split()[0]), float(text.split()[1])
                await db.execute("UPDATE users SET boost=? WHERE uid=?", (boost, uid))
                await db.commit()
                await update.message.reply_text(f"✅  Boost {uid} → x{boost}", reply_markup=kb_admin())
            elif action == "broadcast":
                uids = await all_users()
                ok = fail = 0
                for uid in uids:
                    try:
                        await ctx.bot.send_message(uid, f"📢  {text}", parse_mode="HTML")
                        ok += 1
                    except: fail += 1
                await update.message.reply_text(f"📢  Sent: {ok}, failed: {fail}", reply_markup=kb_admin())
            elif action == "ban":
                uid = int(text)
                await db.execute("UPDATE users SET banned=1 WHERE uid=?", (uid,))
                await db.commit()
                await update.message.reply_text(f"🚫  {uid} banned.", reply_markup=kb_admin())
            elif action == "unban":
                uid = int(text)
                await db.execute("UPDATE users SET banned=0 WHERE uid=?", (uid,))
                await db.commit()
                await update.message.reply_text(f"✅  {uid} unbanned.", reply_markup=kb_admin())
            elif action == "reset":
                if text.upper() == "ALL":
                    await db.execute("UPDATE users SET balance=0")
                else:
                    await db.execute("UPDATE users SET balance=0 WHERE uid=?", (int(text),))
                await db.commit()
                await update.message.reply_text("🗑  Done.", reply_markup=kb_admin())
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ══════════════════════════════════════════════════
# AUTO-FARM BACKGROUND LOOP
# ══════════════════════════════════════════════════
async def auto_farm_loop(app):
    """Every 3 minutes: auto-claim for all users whose timer expired"""
    await asyncio.sleep(30)  # initial delay
    while True:
        try:
            async with aiosqlite.connect(DB) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM users WHERE banned=0 AND farm_end <= ?",
                    (time.time(),)
                ) as c:
                    due = await c.fetchall()

            for u in due:
                ok, reward = await farm_claim(u["uid"])
                if ok:
                    try:
                        usr2 = await user_get(u["uid"])
                        await app.bot.send_message(
                            u["uid"],
                            f"⛏️  <b>Auto-mined!</b>\n\n"
                            f"⭐  +{reward:,.2f} {COIN}\n"
                            f"🔥  Streak: {usr2['streak']}d\n"
                            f"💰  Balance: {usr2['balance']:,.2f} {COIN}\n\n"
                            f"⏳  Next claim in {FARM_HOURS}h.",
                            parse_mode="HTML",
                            reply_markup=kb_main()
                        )
                    except:
                        pass
        except Exception as e:
            logging.error(f"Auto-farm error: {e}")
        await asyncio.sleep(180)  # check every 3 minutes

# ══════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════
def main():
    async def post_init(app):
        await db_init()
        asyncio.create_task(auto_farm_loop(app))
        logging.info("✅ Bot ready — real farming mechanics active")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("menu",  cmd_start))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_adm))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
