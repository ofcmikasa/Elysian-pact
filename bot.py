import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import time
import socket
import unicodedata
import random
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import timedelta, datetime, timezone
from collections import defaultdict

TOKEN = os.environ.get("DISCORD_TOKEN")
OWNER_ID = 1456572804815261858

# ─── KEEP-ALIVE (UptimeRobot) ─────────────────────────────────────────────────


class _ReuseServer(HTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        # Force SO_REUSEADDR + SO_REUSEPORT so restarts don't get blocked by TIME_WAIT
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Elysian is alive and guarding the library.")

    def log_message(self, *args):
        pass


def _start_keep_alive(port: int | None = None):
    # Render (and other PaaS hosts) inject a PORT env var. Fall back to 8080 for local/Replit.
    port = port or int(os.environ.get("PORT", 8080))
    try:
        server = _ReuseServer(("0.0.0.0", port), _Ping)
    except OSError as e:
        print(f"⚠️  Keep-alive port {port} unavailable ({e}); bot will run without it.")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"✧ Keep-alive server running on port {port}")


_start_keep_alive()

# ─── GEMINI ───────────────────────────────────────────────────────────────────

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
gemini_model = None
if GEMINI_KEY:
    try:
        import google.generativeai as genai

        genai.configure(api_key=GEMINI_KEY)
        gemini_model = genai.GenerativeModel("gemini-2.5-flash")
    except Exception as e:
        print(f"Gemini init failed: {e}")

# ─── DATA FILES ───────────────────────────────────────────────────────────────

WARNS_FILE = "warns.json"
USERS_FILE = "users.json"
SHOP_FILE = "shop.json"
CONFIG_FILE = "guild_config.json"
SESSIONS_FILE = "sessions.json"
TASKS_FILE = "tasks.json"
VOWS_FILE = "vows.json"
LEDGER_FILE = "ledger.json"
RAID_FILE = "raid.json"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


warns_db = load_json(WARNS_FILE, {})
users_db = load_json(USERS_FILE, {})
shop_db = load_json(SHOP_FILE, {"items": []})
guild_cfg = load_json(CONFIG_FILE, {})
sessions_db = load_json(SESSIONS_FILE, {})
tasks_db = load_json(TASKS_FILE, {})  # message_id -> task record
vows_db = load_json(VOWS_FILE, {})    # uid -> vow record
ledger_db = load_json(LEDGER_FILE, {})  # uid -> {tag: {oracle:n, fail:n, mastered:bool}}
raid_db = load_json(RAID_FILE, {})    # guild_id -> raid record

# ─── RUNTIME STATE ────────────────────────────────────────────────────────────

invite_cache = {}
message_tracker = defaultdict(list)
study_warn_cd = {}
voice_join_times = {}
pomodoro_tasks = {}
deepwork_tasks = {}
gambit_tasks = {}                     # uid -> asyncio.Task
gambit_state = {}                     # uid -> {start, minutes, bet, topic}
burnout_locks = {}                    # uid -> unlock_timestamp
oracle_challenge_state = {}           # uid -> {turns, concept, history}
duel_state = {}                       # message_id -> {p1, p2, answer, claimed}

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

HOUR_TIERS = [
    (100, "100h Immortal"),
    (50, "50h Sage"),
    (10, "10h Scholar"),
]
TIER_COLORS = {"10h Scholar": 0xC0C0C0, "50h Sage": 0x4169E1, "100h Immortal": 0xFFD700}
HOUR_MILESTONES = [1000, 2500, 5000, 10000]

# High-stakes constants
VOW_DEFAULT_ANTE = 1500
VOW_DEFAULT_DAILY_GOAL = 180          # minutes/day
VOW_FINAL_STAND_HOURS = 72            # last 72h = Final Stand
GAMBIT_DEFAULT_BET = 500
GAMBIT_DEFAULT_MINUTES = 120
GAMBIT_REWARD_MULT = 3                # complete 2h gambit = bet x3
DUEL_ANTE = 100
BURNOUT_LIMIT_MIN = 180               # 3h continuous focus today
BURNOUT_LOCKOUT_SEC = 15 * 60         # 15 min cool-down
RAID_DEFAULT_HP = 10000
RAID_DURATION_DAYS = 7
VOWED_ROLE = "✦ The Vowed"
SAGE_BADGE_ROLE = "📜 Sage of the Library"
COMEBACK_COLOR = 0x8B0000             # crimson

# ─── HELPERS ──────────────────────────────────────────────────────────────────


def get_user(uid: str) -> dict:
    uid = str(uid)
    if uid not in users_db:
        users_db[uid] = {
            "ink": 0,
            "total_hours": 0.0,
            "streak": 0,
            "last_study_date": None,
            "last_daily": None,
            "focus_today": 0.0,
            "focus_today_date": None,
        }
    return users_db[uid]


def add_focus_minutes(uid: str, minutes: float) -> float:
    """Track today's focus minutes for burnout + vow goal. Returns today's total."""
    data = get_user(uid)
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("focus_today_date") != today:
        data["focus_today_date"] = today
        data["focus_today"] = 0.0
    data["focus_today"] = round(data.get("focus_today", 0.0) + minutes, 2)
    save_json(USERS_FILE, users_db)
    return data["focus_today"]


def get_today_focus(uid: str) -> float:
    data = get_user(uid)
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("focus_today_date") != today:
        return 0.0
    return data.get("focus_today", 0.0)


# ── VOW (Blood Pact) helpers ──
def get_vow(uid: str) -> dict | None:
    v = vows_db.get(str(uid))
    return v if v and v.get("active") else None


def vow_deadline_dt(vow: dict) -> datetime:
    return datetime.fromisoformat(vow["deadline"]).replace(tzinfo=timezone.utc)


def is_in_final_stand(uid: str) -> bool:
    v = get_vow(uid)
    if not v:
        return False
    remaining = (vow_deadline_dt(v) - datetime.now(timezone.utc)).total_seconds()
    return 0 < remaining <= VOW_FINAL_STAND_HOURS * 3600


def vow_days_left(vow: dict) -> int:
    remaining = vow_deadline_dt(vow) - datetime.now(timezone.utc)
    return max(0, remaining.days + (1 if remaining.seconds > 0 else 0))


# ── Ledger helpers (Weakness tracker) ──
def add_ledger_event(uid: str, tag: str, *, oracle: bool = False, fail: bool = False, mastered: bool = False):
    uid = str(uid)
    tag = (tag or "general").strip().title()[:30] or "General"
    user_ledger = ledger_db.setdefault(uid, {})
    entry = user_ledger.setdefault(tag, {"oracle": 0, "fail": 0, "mastered": False})
    if oracle:
        entry["oracle"] += 1
    if fail:
        entry["fail"] += 1
    if mastered:
        entry["mastered"] = True
    save_json(LEDGER_FILE, ledger_db)


def topic_heat(entry: dict) -> tuple[str, str]:
    """Return (icon, label) for a ledger entry."""
    if entry.get("mastered"):
        return ("🧊", "Cold (Mastered)")
    weight = entry.get("oracle", 0) + entry.get("fail", 0) * 2
    if weight >= 6:
        return ("🔥", "Burning (Critical)")
    if weight >= 3:
        return ("♨️", "Warm (Practising)")
    return ("✨", "Cool (Touched)")


# ── Burnout helpers ──
def is_burnout_locked(uid: str) -> tuple[bool, int]:
    until = burnout_locks.get(str(uid), 0)
    remaining = int(until - time.time())
    if remaining <= 0:
        burnout_locks.pop(str(uid), None)
        return (False, 0)
    return (True, remaining)


def trigger_burnout_lock(uid: str) -> int:
    burnout_locks[str(uid)] = time.time() + BURNOUT_LOCKOUT_SEC
    return BURNOUT_LOCKOUT_SEC


# ── Raid helpers ──
def get_raid(guild_id: int) -> dict | None:
    r = raid_db.get(str(guild_id))
    if not r or not r.get("active"):
        return None
    if time.time() > r.get("expires_at", 0):
        return None
    return r


def damage_raid(guild_id: int, dmg: float) -> dict | None:
    r = get_raid(guild_id)
    if not r:
        return None
    r["hp"] = max(0, r["hp"] - dmg)
    save_json(RAID_FILE, raid_db)
    return r


def gcfg(guild_id: int) -> dict:
    return guild_cfg.setdefault(str(guild_id), {})


def vault_cid(guild_id: int):
    return gcfg(guild_id).get("vault_channel_id")


async def get_vault(guild: discord.Guild):
    cid = vault_cid(guild.id)
    return guild.get_channel(cid) if cid else None


def clean_nickname(name: str) -> str:
    cleaned = "".join(
        c for c in name if not unicodedata.category(c).startswith("C")
    ).strip()
    return cleaned or "Scholar"


def fill_vars(text: str, member: discord.Member) -> str:
    return (
        text.replace("{user.name}", member.display_name)
        .replace("{user.mention}", member.mention)
        .replace("{user.id}", str(member.id))
        .replace("{server.name}", member.guild.name)
        .replace("{server.members}", str(member.guild.member_count))
    )


def server_total_hours() -> float:
    return sum(d.get("total_hours", 0) for d in users_db.values())


def current_tier(hours: float) -> str:
    for req, name in HOUR_TIERS:
        if hours >= req:
            return name
    return "Unranked"


def streak_icon(s: int) -> str:
    return "🔥🔥" if s >= 7 else ("🔥" if s >= 3 else "")


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


owner_only = app_commands.check(lambda i: i.user.id == OWNER_ID)


async def auto_role(member: discord.Member, hours: float):
    earned = current_tier(hours)
    if earned == "Unranked" or discord.utils.get(member.roles, name=earned):
        return
    guild = member.guild
    for _, name in HOUR_TIERS:
        old = discord.utils.get(guild.roles, name=name)
        if old and old in member.roles:
            try:
                await member.remove_roles(old)
            except Exception:
                pass
    new_role = discord.utils.get(guild.roles, name=earned)
    if not new_role:
        return
    try:
        await member.add_roles(new_role)
    except Exception:
        return
    cfg = gcfg(guild.id)
    cid = cfg.get("leaderboard_channel_id") or cfg.get("vault_channel_id")
    ch = guild.get_channel(cid) if cid else None
    if ch:
        em = discord.Embed(
            title="✨ Tier Ascension",
            description=f"**{member.mention}** has ascended to **{new_role.mention}**!\n*The library acknowledges your dedication.*",
            color=TIER_COLORS.get(earned, 0x7B5EA7),
        )
        em.set_thumbnail(url=member.display_avatar.url)
        em.set_footer(text="Elysian Prestige System")
        await ch.send(embed=em)


async def check_server_milestone(guild: discord.Guild):
    cfg = gcfg(guild.id)
    total = server_total_hours()
    last = cfg.get("last_milestone", 0)
    for ms in HOUR_MILESTONES:
        if total >= ms > last:
            cfg["last_milestone"] = ms
            save_json(CONFIG_FILE, guild_cfg)
            cid = cfg.get("leaderboard_channel_id") or cfg.get("vault_channel_id")
            ch = guild.get_channel(cid) if cid else None
            if ch:
                em = discord.Embed(
                    title=f"🎉 MILESTONE — {ms:,} HOURS STUDIED!",
                    description=(
                        f"**The Elysian Library has collectively studied {ms:,} hours!**\n\n"
                        f"✦ Every candle lit, every page turned — it all counts.\n"
                        f"*The Architect is proud. The library grows eternal.*"
                    ),
                    color=0xFFD700,
                )
                em.set_footer(text="Elysian Prestige System • Digital Party 🎊")
                await ch.send("@everyone", embed=em)
            break


async def mourn_streak(member: discord.Member, old_streak: int):
    if old_streak < 2:
        return
    try:
        await member.send(
            f"📖 *Scholar… the library mourns.*\n"
            f"Your **{old_streak}-day study streak** has ended. The candles grow cold.\n"
            f"But all is not lost — begin again today and rebuild your legacy."
        )
    except Exception:
        pass


# ─── BOT ──────────────────────────────────────────────────────────────────────


class Elysian(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="e!", intents=intents)

    async def setup_hook(self):
        self.add_view(TaskDoneView())  # register persistent task button
        await self.tree.sync()
        self.passive_ink_task.start()
        self.daily_task_check.start()
        self.vow_morning_check.start()
        self.vow_midnight_check.start()
        print("✧ Elysian is online. Guardian of the Library is active. ✧")

    @tasks.loop(hours=24)
    async def vow_morning_check(self):
        """At 10:00 UTC each day, DM scholars who haven't started focusing yet."""
        for uid, vow in list(vows_db.items()):
            if not vow.get("active"):
                continue
            if get_today_focus(uid) > 0:
                continue
            try:
                user = await self.fetch_user(int(uid))
                await user.send(
                    "🌒 *Scholar, the morning has come and you have not opened a single tome.*\n"
                    f"Your Vow demands **{vow.get('daily_goal_minutes', VOW_DEFAULT_DAILY_GOAL)} minutes** of focus today.\n"
                    "Use **`/focus`** now — or the Library will know you have wavered."
                )
            except Exception:
                pass

    @vow_morning_check.before_loop
    async def before_vow_morning(self):
        await self.wait_until_ready()
        now = datetime.now(timezone.utc)
        target = now.replace(hour=10, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    @tasks.loop(hours=24)
    async def vow_midnight_check(self):
        """At UTC midnight: burn Ink for scholars who missed daily goal, expire vows, decay raids."""
        # ── Expire raids past their window ──
        for gid, raid in list(raid_db.items()):
            if raid.get("active") and time.time() > raid.get("expires_at", 0) and raid.get("hp", 0) > 0:
                raid["active"] = False
                raid["result"] = "lost"
                save_json(RAID_FILE, raid_db)
                guild = self.get_guild(int(gid))
                if guild:
                    cfg = gcfg(guild.id)
                    cid = cfg.get("vault_channel_id") or cfg.get("leaderboard_channel_id")
                    ch = guild.get_channel(cid) if cid else None
                    if ch:
                        try:
                            await ch.send(embed=discord.Embed(
                                title="💀 The Boss Stands Triumphant",
                                description=(
                                    f"**{raid['name']}** could not be defeated in time.\n"
                                    "*The library weeps. Daily blessings will be **halved** for the next 7 days.*"
                                ),
                                color=0x8B0000,
                            ))
                        except Exception:
                            pass

        # ── Vow daily-goal enforcement ──
        for uid, vow in list(vows_db.items()):
            if not vow.get("active"):
                continue
            goal = vow.get("daily_goal_minutes", VOW_DEFAULT_DAILY_GOAL)
            done = get_today_focus(uid)
            user = None
            try:
                user = await self.fetch_user(int(uid))
            except Exception:
                pass

            if done < goal:
                penalty = max(50, vow.get("ante", VOW_DEFAULT_ANTE) // 10)
                data = get_user(uid)
                actual = min(penalty, data["ink"])
                data["ink"] -= actual
                vow.setdefault("misses", 0)
                vow["misses"] += 1
                vow.setdefault("burned", 0)
                vow["burned"] += actual
                save_json(USERS_FILE, users_db)
                save_json(VOWS_FILE, vows_db)

                # Public shame
                gid = vow.get("guild_id")
                guild = self.get_guild(int(gid)) if gid else None
                if guild:
                    cfg = gcfg(guild.id)
                    cid = cfg.get("vault_channel_id") or cfg.get("leaderboard_channel_id")
                    ch = guild.get_channel(cid) if cid else None
                    if ch and user:
                        try:
                            await ch.send(embed=discord.Embed(
                                title="🩸 A Vow Is Wavering",
                                description=(
                                    f"Scholar **{user.display_name}** missed their daily goal.\n"
                                    f"**{actual:,} Ink** burned from their pact. *The library is watching.*"
                                ),
                                color=COMEBACK_COLOR,
                            ))
                        except Exception:
                            pass
                if user:
                    try:
                        await user.send(
                            "🩸 *You did not honour your Vow today.*\n"
                            f"**{actual:,} Ink** has been burned from your stake. "
                            f"You needed **{goal} min** of focus — you logged **{done:.0f} min**."
                        )
                    except Exception:
                        pass

            # ── Vow expiry ──
            if datetime.now(timezone.utc) >= vow_deadline_dt(vow):
                vow["active"] = False
                vow["completed_at"] = datetime.now(timezone.utc).isoformat()
                # Return remaining ante (minus what was burned)
                ante = vow.get("ante", VOW_DEFAULT_ANTE)
                burned = vow.get("burned", 0)
                refund = max(0, ante - burned)
                if refund > 0:
                    data = get_user(uid)
                    data["ink"] += refund
                    save_json(USERS_FILE, users_db)
                save_json(VOWS_FILE, vows_db)
                if user:
                    try:
                        await user.send(
                            f"⚱️ *The Pact has reached its end.*\n"
                            f"You met your daily goal **{vow.get('daily_goal_minutes', VOW_DEFAULT_DAILY_GOAL)} min** "
                            f"on most days. Returned: **{refund:,} Ink**."
                        )
                    except Exception:
                        pass
                # Strip "The Vowed" role
                gid = vow.get("guild_id")
                guild = self.get_guild(int(gid)) if gid else None
                if guild and user:
                    member = guild.get_member(int(uid))
                    role = discord.utils.get(guild.roles, name=VOWED_ROLE)
                    if member and role and role in member.roles:
                        try:
                            await member.remove_roles(role)
                        except Exception:
                            pass

    @vow_midnight_check.before_loop
    async def before_vow_midnight(self):
        await self.wait_until_ready()
        now = datetime.now(timezone.utc)
        target = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep((target - now).total_seconds())

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="🏛️ The Library Hall"
            ),
        )
        for guild in self.guilds:
            try:
                invites = await guild.fetch_invites()
                invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
            except Exception:
                pass

    @tasks.loop(minutes=60)
    async def passive_ink_task(self):
        for uid in list(voice_join_times.keys()):
            d = get_user(uid)
            d["ink"] += 10
            d["total_hours"] = round(d.get("total_hours", 0) + 1, 2)
        save_json(USERS_FILE, users_db)

    @passive_ink_task.before_loop
    async def before_passive_ink(self):
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def daily_task_check(self):
        """Every 24 hours, penalise scholars who did not complete their tasks."""
        today = datetime.now(timezone.utc).date().isoformat()
        to_remove = []
        for msg_id, task in tasks_db.items():
            if task.get("done") or task.get("date") == today:
                continue
            uid = task.get("user_id")
            if not uid:
                continue
            data = get_user(uid)
            penalty = task.get("ink_stake", 5)
            data["ink"] = max(0, data["ink"] - penalty)
            save_json(USERS_FILE, users_db)

            # Try to DM the scholar
            try:
                user = await self.fetch_user(int(uid))
                await user.send(
                    f"📖 *Scholar, greatness requires discipline.*\n"
                    f"You did not complete your task: **{task['task']}**\n"
                    f"**{penalty} Ink** has been deducted from your balance. Rise tomorrow."
                )
            except Exception:
                pass

            # Try to update the original message
            try:
                guild = self.get_guild(int(task["guild_id"]))
                ch = guild.get_channel(int(task["channel_id"])) if guild else None
                msg = await ch.fetch_message(int(msg_id)) if ch else None
                if msg:
                    em = msg.embeds[0] if msg.embeds else discord.Embed()
                    em.color = 0xED4245
                    em.set_footer(text=f"❌ Not completed — {penalty} Ink deducted.")
                    await msg.edit(embed=em, view=None)
            except Exception:
                pass

            to_remove.append(msg_id)

        for mid in to_remove:
            tasks_db.pop(mid, None)
        save_json(TASKS_FILE, tasks_db)

    @daily_task_check.before_loop
    async def before_daily_check(self):
        await self.wait_until_ready()
        # Wait until next midnight UTC
        now = datetime.now(timezone.utc)
        next_midnight = datetime(
            now.year, now.month, now.day, tzinfo=timezone.utc
        ) + timedelta(days=1)
        await asyncio.sleep((next_midnight - now).total_seconds())


bot = Elysian()


@bot.tree.error
async def _on_app_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CheckFailure):
        msg = "Access denied."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return
    raise error


# ─── EVENTS ───────────────────────────────────────────────────────────────────


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild is None:
        await handle_dm_chat(message)
        return

    await bot.process_commands(message)
    now = time.time()
    uid = str(message.author.id)

    images = sum(
        1
        for a in message.attachments
        if any(
            a.filename.lower().endswith(x)
            for x in [".png", ".jpg", ".jpeg", ".gif", ".webp"]
        )
    )
    if images >= 5:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} The Gallery is full. Please wait a moment.",
                delete_after=8,
            )
        except Exception:
            pass
        return

    message_tracker[uid].append(now)
    message_tracker[uid] = [t for t in message_tracker[uid] if now - t < 60]
    if len(message_tracker[uid]) >= 10:
        last_warned = study_warn_cd.get(uid, 0)
        if now - last_warned > 600:
            study_warn_cd[uid] = now
            message_tracker[uid] = []
            try:
                await message.author.send(
                    "📚 *Scholar, your books are waiting.* You've been very active in chat. Shall I mute this channel for you so you can focus?"
                )
            except Exception:
                pass


@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    vault = await get_vault(message.guild)
    if not vault:
        return
    if message.mentions:
        pinged = ", ".join(m.mention for m in message.mentions)
        em = discord.Embed(title="👻 Ghost Ping Detected", color=0xFF6B6B)
        em.add_field(
            name="Pinger",
            value=f"{message.author.mention} (`{message.author.id}`)",
            inline=False,
        )
        em.add_field(name="Pinged", value=pinged, inline=False)
        em.add_field(name="Channel", value=message.channel.mention, inline=False)
        em.add_field(
            name="Deleted Message", value=message.content or "*(no text)*", inline=False
        )
        em.set_footer(text="Elysian Vault • Ghost Ping")
        await vault.send(embed=em)
    else:
        em = discord.Embed(title="🗑️ Message Deleted", color=0xFF4D4D)
        em.add_field(
            name="Scholar",
            value=f"{message.author.mention} (`{message.author.id}`)",
            inline=False,
        )
        em.add_field(name="Location", value=message.channel.mention, inline=False)
        em.add_field(
            name="Content", value=message.content or "*(attachment only)*", inline=False
        )
        em.set_footer(text="Elysian Vault • Deleted Message")
        await vault.send(embed=em)
    for att in message.attachments:
        if any(
            att.filename.lower().endswith(x)
            for x in [".png", ".jpg", ".jpeg", ".gif", ".webp"]
        ):
            em = discord.Embed(
                title="📷 Vanished Media Recovered",
                description=f"Deleted by {message.author.mention} in {message.channel.mention}",
                color=0xFFA500,
            )
            em.set_image(url=att.url)
            em.set_footer(text="Elysian Vault • Vanished Media")
            await vault.send(embed=em)


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content:
        return
    vault = await get_vault(before.guild)
    if not vault:
        return
    em = discord.Embed(title="✏️ Shadow Edit Detected", color=0xFFCC00)
    em.add_field(name="Scholar", value=before.author.mention, inline=False)
    em.add_field(name="Original", value=before.content or "*(empty)*", inline=False)
    em.add_field(name="Revised", value=after.content or "*(empty)*", inline=False)
    em.add_field(name="Channel", value=before.channel.mention, inline=False)
    em.set_footer(text="Elysian Vault • Shadow Edit")
    await vault.send(embed=em)


@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        vault = await get_vault(after.guild)
        if vault:
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            if added:
                em = discord.Embed(title="🎭 Role Stealth — Role Added", color=0x57F287)
                em.add_field(name="Member", value=after.mention, inline=False)
                em.add_field(
                    name="Role Added",
                    value=", ".join(r.mention for r in added),
                    inline=False,
                )
                em.set_footer(text="Elysian Vault • Role Stealth")
                await vault.send(embed=em)
            if removed:
                em = discord.Embed(
                    title="🎭 Role Stealth — Role Removed", color=0xED4245
                )
                em.add_field(name="Member", value=after.mention, inline=False)
                em.add_field(
                    name="Role Removed",
                    value=", ".join(r.mention for r in removed),
                    inline=False,
                )
                em.set_footer(text="Elysian Vault • Role Stealth")
                await vault.send(embed=em)
    cleaned = clean_nickname(after.display_name)
    if cleaned != after.display_name:
        try:
            await after.edit(nick=cleaned, reason="Elysian Auto-Nickname")
        except Exception:
            pass


@bot.event
async def on_member_join(member):
    cleaned = clean_nickname(member.display_name)
    if cleaned != member.display_name:
        try:
            await member.edit(nick=cleaned, reason="Elysian Auto-Nickname")
        except Exception:
            pass
    guild = member.guild
    cfg = gcfg(guild.id)
    tmpl = cfg.get("welcome_template")
    if tmpl:
        cid = cfg.get("welcome_channel_id") or cfg.get("leaderboard_channel_id")
        ch = guild.get_channel(cid) if cid else None
        if ch:
            try:
                color = int(tmpl.get("color", "7B5EA7"), 16)
            except Exception:
                color = 0x7B5EA7
            em = discord.Embed(
                title=fill_vars(tmpl.get("title", "Welcome!"), member),
                description=fill_vars(tmpl.get("description", ""), member),
                color=color,
            )
            em.set_thumbnail(url=member.display_avatar.url)
            if tmpl.get("image"):
                em.set_image(url=tmpl["image"])
            if tmpl.get("footer"):
                em.set_footer(text=fill_vars(tmpl["footer"], member))
            await ch.send(embed=em)
    vault = await get_vault(guild)
    if not vault:
        return
    try:
        new_invites = await guild.fetch_invites()
        new_map = {inv.code: inv.uses for inv in new_invites}
        used = None
        for code, uses in new_map.items():
            if uses > invite_cache.get(guild.id, {}).get(code, 0):
                used = next((inv for inv in new_invites if inv.code == code), None)
                break
        invite_cache[guild.id] = new_map
        em = discord.Embed(title="🔗 Invite Watch — New Member", color=0x5865F2)
        em.set_thumbnail(url=member.display_avatar.url)
        em.add_field(
            name="Member", value=f"{member.mention} (`{member.id}`)", inline=False
        )
        if used:
            em.add_field(name="Invite Code", value=f"`{used.code}`", inline=True)
            em.add_field(
                name="Created By",
                value=used.inviter.mention if used.inviter else "?",
                inline=True,
            )
            em.add_field(name="Total Uses", value=str(used.uses), inline=True)
        else:
            em.add_field(name="Invite", value="Could not determine.", inline=False)
        em.set_footer(text="Elysian Vault • Invite Watch")
        await vault.send(embed=em)
    except Exception:
        pass


@bot.event
async def on_member_remove(member):
    cfg = gcfg(member.guild.id)
    tmpl = cfg.get("goodbye_template")
    if not tmpl:
        return
    cid = cfg.get("welcome_channel_id") or cfg.get("leaderboard_channel_id")
    ch = member.guild.get_channel(cid) if cid else None
    if not ch:
        return
    try:
        color = int(tmpl.get("color", "ed4245"), 16)
    except Exception:
        color = 0xED4245
    em = discord.Embed(
        title=fill_vars(tmpl.get("title", "Farewell!"), member),
        description=fill_vars(tmpl.get("description", ""), member),
        color=color,
    )
    em.set_thumbnail(url=member.display_avatar.url)
    if tmpl.get("footer"):
        em.set_footer(text=fill_vars(tmpl["footer"], member))
    await ch.send(embed=em)


@bot.event
async def on_voice_state_update(member, before, after):
    uid = str(member.id)
    cfg = gcfg(member.guild.id)
    svcs = cfg.get("study_vc_ids", [])

    joined = (
        after.channel
        and after.channel.id in svcs
        and (not before.channel or before.channel.id not in svcs)
    )
    left = (
        before.channel
        and before.channel.id in svcs
        and (not after.channel or after.channel.id not in svcs)
    )

    if joined:
        voice_join_times[uid] = time.time()
    if left:
        join_ts = voice_join_times.pop(uid, None)
        if not join_ts:
            return
        elapsed_h = (time.time() - join_ts) / 3600
        data = get_user(uid)
        old_streak = data.get("streak", 0)
        data["ink"] += int(elapsed_h * 10)
        data["total_hours"] = round(data.get("total_hours", 0) + elapsed_h, 2)
        today = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        last = data.get("last_study_date")
        if last and last != today and last != yesterday:
            await mourn_streak(member, old_streak)
            data["streak"] = 1
        elif last == yesterday:
            data["streak"] = old_streak + 1
        elif last != today:
            data["streak"] = 1
        data["last_study_date"] = today
        if data["streak"] % 3 == 0:
            data["ink"] += 5
        save_json(USERS_FILE, users_db)
        await auto_role(member, data["total_hours"])
        await check_server_milestone(member.guild)


# ═══════════════════════════════════════════════════════════════════════════════
#  PERSISTENT VIEWS
# ═══════════════════════════════════════════════════════════════════════════════


class TaskDoneView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Mark as Done",
        style=discord.ButtonStyle.success,
        custom_id="task_done_btn",
        emoji="✅",
    )
    async def mark_done(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        msg_id = str(interaction.message.id)
        task = tasks_db.get(msg_id)

        if not task:
            return await interaction.response.send_message(
                "This task record no longer exists.", ephemeral=True
            )
        if task.get("user_id") != str(interaction.user.id):
            return await interaction.response.send_message(
                "That's not your task, scholar.", ephemeral=True
            )
        if task.get("done"):
            return await interaction.response.send_message(
                "You already completed this task! 🎉", ephemeral=True
            )

        task["done"] = True
        reward = 25
        data = get_user(task["user_id"])
        data["ink"] += reward
        save_json(USERS_FILE, users_db)
        save_json(TASKS_FILE, tasks_db)

        await auto_role(interaction.user, data["total_hours"])

        # Update the embed
        em = interaction.message.embeds[0].copy()
        em.color = 0x57F287
        em.set_footer(
            text=f"✅ Completed — +{reward} Ink rewarded! Total: {data['ink']:,} Ink"
        )
        button.disabled = True
        button.label = "✅ Completed!"
        await interaction.message.edit(embed=em, view=self)

        await interaction.response.send_message(
            f"🎉 Task complete! **+{reward} Ink** added to your balance. Keep the momentum going, scholar.",
            ephemeral=True,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  MODALS
# ═══════════════════════════════════════════════════════════════════════════════


class PurgeModal(discord.ui.Modal, title="🗑️ Cleanse Messages"):
    amount = discord.ui.TextInput(
        label="Number of messages to delete", placeholder="e.g. 50", max_length=4
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.amount.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=n)
        await interaction.followup.send(
            f"Cleansed **{len(deleted)}** messages.", ephemeral=True
        )


class PurgeUserModal(discord.ui.Modal, title="🗑️ Cleanse User Messages"):
    amount = discord.ui.TextInput(
        label="Messages to scan", placeholder="e.g. 100", max_length=4
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.amount.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(
            limit=n, check=lambda m: m.author.id == self.target.id
        )
        await interaction.followup.send(
            f"Removed **{len(deleted)}** messages from **{self.target.display_name}**.",
            ephemeral=True,
        )


class SlowmodeModal(discord.ui.Modal, title="⏱️ Set Slowmode"):
    seconds = discord.ui.TextInput(
        label="Delay in seconds (0 to disable)", placeholder="e.g. 10", max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            s = int(self.seconds.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        await interaction.channel.edit(slowmode_delay=s)
        msg = "⏱️ Slowmode lifted." if s == 0 else f"⏱️ Slowmode set to **{s}s**."
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, color=0xFFA500)
        )


class NukeModal(discord.ui.Modal, title="🌌 Confirm Channel Nuke"):
    confirm = discord.ui.TextInput(
        label='Type "NUKE" to confirm', placeholder="NUKE", max_length=4
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm.value.strip().upper() != "NUKE":
            return await interaction.response.send_message(
                "Nuke cancelled.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        ch = interaction.channel
        new_ch = await ch.clone(reason="Elysian Nuke")
        await new_ch.edit(position=ch.position)
        await ch.delete(reason="Elysian Nuke")
        await new_ch.send(
            "🌌 *The library has been purified. A new chapter begins.*", delete_after=12
        )


class MuteModal(discord.ui.Modal, title="🌿 Silence Scholar"):
    minutes = discord.ui.TextInput(
        label="Duration (minutes)", placeholder="e.g. 10", max_length=4
    )
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Why are you silencing them?",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        try:
            m = int(self.minutes.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        reason = self.reason.value.strip() or "No reason provided"
        try:
            await self.target.timeout(timedelta(minutes=m), reason=reason)
            try:
                await self.target.send(
                    f"🌿 Moved to the Silent Gardens for: **{reason}**\nDuration: **{m} minute(s)**."
                )
            except Exception:
                pass
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"🌿 {self.target.mention} entered the Silent Gardens for **{m}m**.\nReason: *{reason}*",
                    color=0x7B5EA7,
                )
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I lack the authority to silence this scholar.", ephemeral=True
            )


class WarnModal(discord.ui.Modal, title="📜 Issue Warning"):
    reason = discord.ui.TextInput(
        label="Reason for warning",
        placeholder="What did they do?",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(self.target.id)
        reason = self.reason.value.strip() or "No reason provided"
        warns_db.setdefault(uid, {"username": str(self.target), "warnings": []})
        warns_db[uid]["warnings"].append(
            {"reason": reason, "timestamp": discord.utils.utcnow().isoformat()}
        )
        save_json(WARNS_FILE, warns_db)
        count = len(warns_db[uid]["warnings"])
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"📜 **{self.target.display_name}** received strike #{count}.\nReason: *{reason}*",
                color=0xFFA500,
            )
        )
        try:
            await self.target.send(
                f"⚠️ Warning in **{interaction.guild.name}**: **{reason}**"
            )
        except Exception:
            pass


class KickModal(discord.ui.Modal, title="👣 Remove Scholar"):
    reason = discord.ui.TextInput(
        label="Reason for removal",
        placeholder="Why are you removing them?",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason.value.strip() or "No reason provided"
        try:
            await self.target.send(
                f"👣 Removed from **{interaction.guild.name}**. Reason: **{reason}**"
            )
        except Exception:
            pass
        await self.target.kick(reason=reason)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"👣 **{self.target.display_name}** escorted out.\nReason: *{reason}*",
                color=0xED4245,
            )
        )


class BanModal(discord.ui.Modal, title="🔒 Exile Scholar"):
    reason = discord.ui.TextInput(
        label="Reason for exile",
        placeholder="Why are you banning them?",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason.value.strip() or "No reason provided"
        try:
            await self.target.send(
                f"🔒 Exiled from **{interaction.guild.name}**. Reason: **{reason}**"
            )
        except Exception:
            pass
        await self.target.ban(reason=reason, delete_message_days=0)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"🔒 **{self.target.display_name}** exiled.\nReason: *{reason}*",
                color=0xED4245,
            )
        )


class SetInkModal(discord.ui.Modal, title="💧 Set Ink Balance"):
    amount = discord.ui.TextInput(
        label="New Ink amount", placeholder="e.g. 500", max_length=10
    )

    def __init__(self, user: discord.Member):
        super().__init__()
        self.target = user

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(self.amount.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        get_user(str(self.target.id))["ink"] = n
        save_json(USERS_FILE, users_db)
        await interaction.response.send_message(
            f"💧 **{self.target.display_name}**'s Ink set to **{n:,}**.", ephemeral=True
        )


class BroadcastModal(discord.ui.Modal, title="📣 Elysian Broadcast"):
    b_title = discord.ui.TextInput(
        label="Announcement Title", placeholder="Important Notice", max_length=256
    )
    b_message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Write your announcement here...",
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        em = discord.Embed(
            title=self.b_title.value, description=self.b_message.value, color=0x7B5EA7
        )
        em.set_footer(text="— The Architect")
        vault_id = vault_cid(interaction.guild.id)
        sent = 0
        for ch in interaction.guild.text_channels:
            if ch.id == vault_id:
                continue
            try:
                await ch.send(embed=em)
                sent += 1
            except Exception:
                pass
        await interaction.followup.send(
            f"📣 Broadcast sent to **{sent}** channels.", ephemeral=True
        )


class FocusModal(discord.ui.Modal, title="📚 Start Focus Session"):
    topic = discord.ui.TextInput(
        label="What are you studying?",
        placeholder="e.g. Mathematics Chapter 5",
        max_length=200,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        topic = self.topic.value.strip() or "Open study"
        if uid in sessions_db:
            elapsed = (time.time() - sessions_db[uid]["start"]) / 60
            return await interaction.response.send_message(
                f"📚 Already in session: *{sessions_db[uid]['topic']}* — {elapsed:.0f}m in. Use `/endfocus` first.",
                ephemeral=True,
            )
        sessions_db[uid] = {"start": time.time(), "topic": topic}
        save_json(SESSIONS_FILE, sessions_db)
        study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
        if study_role:
            try:
                await interaction.user.add_roles(study_role)
            except Exception:
                pass
        em = discord.Embed(
            title="📚 Focus Session Started",
            description=f"**{interaction.user.display_name}** has entered deep focus.\n*Topic: {topic}*",
            color=0x7B5EA7,
        )
        em.set_footer(text="Use /endfocus to end your session and claim your Ink.")
        await interaction.response.send_message(embed=em)


class PomodoroModal(discord.ui.Modal, title="🍅 Pomodoro Timer"):
    work_m = discord.ui.TextInput(
        label="Work duration (minutes)", placeholder="25", max_length=3, default="25"
    )
    break_m = discord.ui.TextInput(
        label="Break duration (minutes)", placeholder="5", max_length=3, default="5"
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if uid in pomodoro_tasks:
            return await interaction.response.send_message(
                "Already have an active Pomodoro. Use `/stoppomodoro` to cancel.",
                ephemeral=True,
            )
        try:
            wm = int(self.work_m.value.strip())
            bm = int(self.break_m.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter valid numbers.", ephemeral=True
            )
        study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
        if study_role:
            try:
                await interaction.user.add_roles(study_role)
            except Exception:
                pass
        task = asyncio.create_task(
            _run_pomodoro(interaction.user, interaction.channel, wm, bm)
        )
        pomodoro_tasks[uid] = task
        em = discord.Embed(
            title="🍅 Pomodoro Started",
            description=f"**Work:** {wm} minutes\n**Break:** {bm} minutes\n\n*Study role applied. The bot will DM you when it's break time.*",
            color=0x7B5EA7,
        )
        em.set_footer(text="Use /stoppomodoro to cancel.")
        await interaction.response.send_message(embed=em)


class DeepWorkModal(discord.ui.Modal, title="🧠 Deep Work Mode"):
    minutes = discord.ui.TextInput(
        label="How long to hide General Chat (minutes)",
        placeholder="60",
        max_length=4,
        default="60",
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        cfg = gcfg(interaction.guild.id)
        cid = cfg.get("general_channel_id")
        if not cid:
            return await interaction.response.send_message(
                "No General Chat configured. Run `/elysian_genesis` first.",
                ephemeral=True,
            )
        ch = interaction.guild.get_channel(cid)
        if not ch:
            return await interaction.response.send_message(
                "General Chat channel not found.", ephemeral=True
            )
        if uid in deepwork_tasks:
            return await interaction.response.send_message(
                "You're already in Deep Work mode.", ephemeral=True
            )
        try:
            m = int(self.minutes.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Please enter a valid number.", ephemeral=True
            )
        ow = ch.overwrites_for(interaction.user)
        ow.read_messages = False
        await ch.set_permissions(interaction.user, overwrite=ow)

        async def wait_and_restore():
            await asyncio.sleep(m * 60)
            ow2 = ch.overwrites_for(interaction.user)
            ow2.read_messages = None
            try:
                await ch.set_permissions(interaction.user, overwrite=ow2)
            except Exception:
                pass
            try:
                await interaction.user.send(
                    f"🔓 **Deep Work complete!** You can now see {ch.mention} again."
                )
            except Exception:
                pass
            deepwork_tasks.pop(uid, None)

        deepwork_tasks[uid] = asyncio.create_task(wait_and_restore())
        em = discord.Embed(
            title="🧠 Deep Work Mode Activated",
            description=f"{ch.mention} is now hidden from you for **{m} minutes**.\n\n*The distraction has been sealed. Return to your books.*",
            color=0x2F3136,
        )
        em.set_footer(text="The channel restores automatically when the timer ends.")
        await interaction.response.send_message(embed=em, ephemeral=True)


class SummarizeModal(discord.ui.Modal, title="🔮 AI Oracle — Summarize"):
    text = discord.ui.TextInput(
        label="Paste your text or article below",
        style=discord.TextStyle.paragraph,
        placeholder="Paste the content you want broken down...",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message(
                "🔮 The AI oracle is not yet awakened. Please add a `GEMINI_API_KEY` secret.",
                ephemeral=True,
            )
        await interaction.response.defer()
        try:
            prompt = (
                "You are the Elysian Oracle, a sophisticated academic assistant. "
                "Analyze the following text and respond with:\n"
                "**3 Key Pillars** (the 3 most important concepts, each on its own line starting with ✦)\n"
                "**5 Key Definitions** (the 5 most important terms, each formatted as **Term** — definition)\n\n"
                f"Text:\n{self.text.value}"
            )
            response = gemini_model.generate_content(prompt)
            em = discord.Embed(
                title="🔮 Oracle Analysis",
                description=response.text.strip(),
                color=0x7B5EA7,
            )
            em.set_footer(text="Elysian Oracle • Powered by Gemini AI")
            await interaction.followup.send(embed=em)
        except Exception as e:
            await interaction.followup.send(
                f"The oracle encountered an error: `{e}`", ephemeral=True
            )


class AskModal(discord.ui.Modal, title="🎓 Ask the Elysian Oracle"):
    question = discord.ui.TextInput(
        label="Your academic question",
        style=discord.TextStyle.paragraph,
        placeholder="Ask anything academic — history, science, literature, maths...",
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message(
                "🔮 The AI oracle is not yet awakened. Please add a `GEMINI_API_KEY` secret.",
                ephemeral=True,
            )
        await interaction.response.defer()
        try:
            prompt = (
                "You are Elysian, a sophisticated and witty academic tutor who speaks with eloquence and scholarly authority. "
                "You reside in a prestigious digital library and guide scholars with precise, insightful, slightly witty answers. "
                "Answer the following academic question in a helpful but characterful way. Be concise but thorough.\n\n"
                f"Question: {self.question.value}"
            )
            response = gemini_model.generate_content(prompt)
            em = discord.Embed(
                title=f"🎓 The Oracle Speaks",
                description=response.text.strip(),
                color=0x7B5EA7,
            )
            em.set_author(
                name=f"Asked by {interaction.user.display_name}",
                icon_url=interaction.user.display_avatar.url,
            )
            em.set_footer(text="Elysian Oracle • Powered by Gemini AI")
            await interaction.followup.send(embed=em)
        except Exception as e:
            await interaction.followup.send(
                f"The oracle encountered an error: `{e}`", ephemeral=True
            )


class TaskModal(discord.ui.Modal, title="✅ Daily Commitment"):
    task_text = discord.ui.TextInput(
        label="Your task for today",
        placeholder="e.g. Finish Biology Chapter 4, solve 10 math problems",
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        cfg = gcfg(interaction.guild.id)
        cid = cfg.get("daily_goals_channel_id") or interaction.channel.id
        ch = interaction.guild.get_channel(cid) or interaction.channel
        today = datetime.now(timezone.utc).date().isoformat()

        # Give initiation Ink
        data = get_user(uid)
        data["ink"] += 5
        save_json(USERS_FILE, users_db)

        em = discord.Embed(
            title="📋 Scholar's Commitment",
            description=f"**{interaction.user.display_name}** has committed to:\n\n*{self.task_text.value}*",
            color=0x7B5EA7,
        )
        em.set_thumbnail(url=interaction.user.display_avatar.url)
        em.add_field(name="📅 Date", value=today, inline=True)
        em.add_field(name="💧 Ink on Entry", value="+5 (initiation)", inline=True)
        em.add_field(name="🏆 Reward", value="+25 Ink on completion", inline=True)
        em.set_footer(text="Click the button below when you've completed this task.")

        view = TaskDoneView()
        msg = await ch.send(embed=em, view=view)

        # Store the task
        tasks_db[str(msg.id)] = {
            "task": self.task_text.value,
            "user_id": uid,
            "guild_id": str(interaction.guild.id),
            "channel_id": str(ch.id),
            "message_id": str(msg.id),
            "done": False,
            "date": today,
            "ink_stake": 10,
        }
        save_json(TASKS_FILE, tasks_db)

        await interaction.response.send_message(
            f"✅ Commitment logged! **+5 Ink** for starting. Complete it for **+25 more Ink**.",
            ephemeral=True,
        )


class ResourceModal(discord.ui.Modal, title="📚 Submit Resource"):
    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="e.g. Biology, Mathematics, History",
        max_length=100,
    )
    topic = discord.ui.TextInput(
        label="Topic",
        placeholder="e.g. Cell Division, Integration, WW2",
        max_length=100,
    )
    res_type = discord.ui.TextInput(
        label="Type", placeholder="Article / Video / PDF / Notes / Tool", max_length=50
    )
    link = discord.ui.TextInput(
        label="Link / Source", placeholder="https://...", max_length=500
    )
    summary = discord.ui.TextInput(
        label="Summary",
        placeholder="What does this resource cover? Why is it useful?",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = gcfg(interaction.guild.id)
        cid = cfg.get("resource_vault_channel_id")
        ch = interaction.guild.get_channel(cid) if cid else interaction.channel

        em = discord.Embed(
            title=f"📚 {self.subject.value} — {self.topic.value}",
            description=self.summary.value,
            color=0xFFD700,
        )
        em.add_field(name="📂 Subject", value=self.subject.value, inline=True)
        em.add_field(name="🏷️ Topic", value=self.topic.value, inline=True)
        em.add_field(name="📄 Type", value=self.res_type.value, inline=True)
        em.add_field(name="🔗 Source", value=self.link.value, inline=False)
        em.set_author(
            name=f"Curated by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        em.set_footer(text="Elysian Resource Vault • Curated Knowledge")
        em.timestamp = discord.utils.utcnow()

        await ch.send(embed=em)
        await interaction.response.send_message(
            f"✅ Resource submitted to {ch.mention}. The library grows.", ephemeral=True
        )


class WelcomeGoodbyeModal(discord.ui.Modal):
    title_field = discord.ui.TextInput(
        label="Embed Title", placeholder="Welcome to {server.name}!", max_length=256
    )
    desc_field = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Hey {user.mention}! We now have {server.members} members.",
        max_length=2048,
    )
    color_field = discord.ui.TextInput(
        label="Color (hex)", placeholder="7B5EA7", max_length=6, required=False
    )
    footer_field = discord.ui.TextInput(
        label="Footer Text",
        placeholder="Optional footer...",
        required=False,
        max_length=200,
    )
    image_field = discord.ui.TextInput(
        label="Image URL", placeholder="https://...", required=False
    )

    def __init__(self, mode: str):
        self._mode = mode
        super().__init__(title=f"Set {mode.title()} Message")

    async def on_submit(self, interaction: discord.Interaction):
        gcfg(interaction.guild.id)[f"{self._mode}_template"] = {
            "title": self.title_field.value,
            "description": self.desc_field.value,
            "color": self.color_field.value.strip().lstrip("#")
            or ("7B5EA7" if self._mode == "welcome" else "ed4245"),
            "footer": self.footer_field.value,
            "image": self.image_field.value.strip(),
        }
        save_json(CONFIG_FILE, guild_cfg)
        await interaction.response.send_message(
            f"✅ {self._mode.title()} message saved!\nVariables: `{{user.name}}` `{{user.mention}}` `{{server.name}}` `{{server.members}}`",
            ephemeral=True,
        )


class TemplateSaveModal(discord.ui.Modal, title="📚 Save Embed Template"):
    tmpl_name = discord.ui.TextInput(
        label="Template name (used to recall it)",
        placeholder="e.g. Rules, Announcement, Welcome",
        max_length=50,
    )
    tmpl_title = discord.ui.TextInput(
        label="Embed title", placeholder="Enter title...", max_length=256
    )
    tmpl_desc = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Supports {user.name} etc.",
        max_length=2048,
    )
    tmpl_color = discord.ui.TextInput(
        label="Color (hex)", placeholder="7B5EA7", max_length=6, required=False
    )
    tmpl_image = discord.ui.TextInput(
        label="Image URL", placeholder="https://...", required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        name = self.tmpl_name.value.strip()
        if not name:
            return await interaction.response.send_message(
                "Template name cannot be empty.", ephemeral=True
            )
        gcfg(interaction.guild.id).setdefault("templates", {})[name] = {
            "title": self.tmpl_title.value,
            "description": self.tmpl_desc.value,
            "color": self.tmpl_color.value.strip().lstrip("#") or "7B5EA7",
            "footer": "",
            "image": self.tmpl_image.value.strip(),
        }
        save_json(CONFIG_FILE, guild_cfg)
        await interaction.response.send_message(
            f"✅ Template **{name}** saved. Use `/template_post` to share it.",
            ephemeral=True,
        )


class EmbedBuilderModal(discord.ui.Modal, title="✨ Elysian Embed Builder"):
    e_title = discord.ui.TextInput(
        label="Title", placeholder="Enter a title...", max_length=256
    )
    e_desc = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        placeholder="Write your message...",
        max_length=2048,
    )
    e_color = discord.ui.TextInput(
        label="Color (hex)", placeholder="7B5EA7", max_length=6, required=False
    )
    e_footer = discord.ui.TextInput(
        label="Footer Text", placeholder="Optional...", required=False, max_length=200
    )
    e_image = discord.ui.TextInput(
        label="Image URL", placeholder="https://...", required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = int(self.e_color.value.strip().lstrip("#") or "7B5EA7", 16)
        except ValueError:
            color = 0x7B5EA7
        em = discord.Embed(
            title=fill_vars(self.e_title.value, interaction.user),
            description=fill_vars(self.e_desc.value, interaction.user),
            color=color,
        )
        if self.e_footer.value:
            em.set_footer(text=fill_vars(self.e_footer.value, interaction.user))
        if self.e_image.value.strip():
            em.set_image(url=self.e_image.value.strip())
        await interaction.response.send_message(embed=em)


class AddItemModal(discord.ui.Modal, title="🛍️ Add Shop Item"):
    item_name = discord.ui.TextInput(
        label="Item name", placeholder="e.g. Study Color — Lavender", max_length=100
    )
    item_price = discord.ui.TextInput(
        label="Price (Ink)", placeholder="e.g. 200", max_length=6
    )
    item_role = discord.ui.TextInput(
        label="Role name to grant",
        placeholder="Exact Discord role name",
        max_length=100,
    )
    item_desc = discord.ui.TextInput(
        label="Description",
        placeholder="Short description",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            price = int(self.item_price.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "Price must be a number.", ephemeral=True
            )
        role = discord.utils.get(
            interaction.guild.roles, name=self.item_role.value.strip()
        )
        if not role:
            return await interaction.response.send_message(
                f"No role named **{self.item_role.value.strip()}** found.",
                ephemeral=True,
            )
        shop_db["items"].append(
            {
                "name": self.item_name.value.strip(),
                "price": price,
                "role_id": role.id,
                "description": self.item_desc.value.strip(),
            }
        )
        save_json(SHOP_FILE, shop_db)
        await interaction.response.send_message(
            f"✅ **{self.item_name.value.strip()}** added for **{price:,} Ink** → {role.mention}.",
            ephemeral=True,
        )


def shop_price_for(guild_id: int, base_price: int) -> tuple[int, str]:
    """Returns (final_price, modifier_label) — applies raid-victory 50% off."""
    raid = raid_db.get(str(guild_id), {})
    if raid.get("result") == "won" and time.time() < raid.get("expires_at", 0) + RAID_DURATION_DAYS * 86400:
        return (max(1, base_price // 2), " (50% RAID VICTORY!)")
    return (base_price, "")


async def _purchase_item(interaction: discord.Interaction, item_idx: int):
    """Shared purchase flow used by the shop dropdown's Buy button."""
    if is_in_final_stand(str(interaction.user.id)):
        return await interaction.response.send_message(
            "🩸 **Final Stand sealed.** The boutique is closed until your Vow is fulfilled.",
            ephemeral=True,
        )
    items = shop_db.get("items", [])
    if item_idx < 0 or item_idx >= len(items):
        return await interaction.response.send_message(
            "That item is no longer available.", ephemeral=True
        )
    item = items[item_idx]
    final_price, _ = shop_price_for(interaction.guild.id, item["price"])
    data = get_user(str(interaction.user.id))
    if data["ink"] < final_price:
        return await interaction.response.send_message(
            f"Not enough Ink. You have **{data['ink']:,}**, need **{final_price:,}**.",
            ephemeral=True,
        )
    data["ink"] -= final_price
    save_json(USERS_FILE, users_db)
    role = interaction.guild.get_role(item.get("role_id", 0))
    if role:
        try:
            await interaction.user.add_roles(role)
        except Exception:
            pass
    em = discord.Embed(
        title="✅ Purchase Complete",
        description=f"You acquired **{item['name']}**.",
        color=0x57F287,
    )
    em.add_field(name="💧 Spent", value=f"{final_price:,}")
    em.add_field(name="💧 Remaining Ink", value=f"{data['ink']:,}")
    if role:
        em.add_field(name="🎭 Role Granted", value=role.mention)
    await interaction.response.send_message(embed=em, ephemeral=True)


class PurchaseButton(discord.ui.View):
    def __init__(self, item_idx: int, price: int):
        super().__init__(timeout=120)
        self.item_idx = item_idx

        async def _buy(interaction: discord.Interaction):
            await _purchase_item(interaction, self.item_idx)

        btn = discord.ui.Button(
            label=f"Confirm Purchase ({price:,} Ink)",
            style=discord.ButtonStyle.success,
            emoji="💎",
        )
        btn.callback = _buy
        self.add_item(btn)


# ═══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

# ─── GENESIS ──────────────────────────────────────────────────────────────────


@bot.tree.command(
    name="elysian_genesis",
    description="Elysian: Build the full Library Hall server structure automatically.",
)
@owner_only
async def elysian_genesis(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    cfg = gcfg(guild.id)

    # ── Roles ──────────────────────────────────────────────────────
    role_specs = [
        ("10h Scholar", discord.Color.from_str("#C0C0C0")),
        ("50h Sage", discord.Color.from_str("#4169E1")),
        ("100h Immortal", discord.Color.from_str("#FFD700")),
        ("Honor Society", discord.Color.from_str("#E74C3C")),
        ("Library VIP", discord.Color.from_str("#9B59B6")),
        ("The Architect's Favorite", discord.Color.from_str("#FF8C00")),
        ("📚 Study", discord.Color.from_str("#7B5EA7")),
    ]
    for name, color in role_specs:
        if not discord.utils.get(guild.roles, name=name):
            await guild.create_role(name=name, color=color, reason="Elysian Genesis")

    # ── Category: 🏁 START HERE ────────────────────────────────────
    cat_start = discord.utils.get(
        guild.categories, name="🏁 START HERE"
    ) or await guild.create_category("🏁 START HERE")

    rules_ch = discord.utils.get(
        guild.text_channels, name="📜-rules-and-info"
    ) or await guild.create_text_channel("📜-rules-and-info", category=cat_start)
    announce_ch = discord.utils.get(
        guild.text_channels, name="📢-announcements"
    ) or await guild.create_text_channel("📢-announcements", category=cat_start)
    cfg["welcome_channel_id"] = rules_ch.id

    # ── Category: 📝 THE STUDY HALL ────────────────────────────────
    cat_study = discord.utils.get(
        guild.categories, name="📝 THE STUDY HALL"
    ) or await guild.create_category("📝 THE STUDY HALL")

    general_ch = discord.utils.get(
        guild.text_channels, name="💬-general-study"
    ) or await guild.create_text_channel("💬-general-study", category=cat_study)
    goals_ch = discord.utils.get(
        guild.text_channels, name="✅daily-goals"
    ) or await guild.create_text_channel("✅daily-goals", category=cat_study)
    resource_ch = discord.utils.get(
        guild.text_channels, name="📚resource-vault"
    ) or await guild.create_text_channel("📚resource-vault", category=cat_study)
    bot_ch = discord.utils.get(
        guild.text_channels, name="🤖bot-commands"
    ) or await guild.create_text_channel("🤖bot-commands", category=cat_study)
    pomo_ch = discord.utils.get(
        guild.text_channels, name="🍅pomodoro-bot"
    ) or await guild.create_text_channel("🍅pomodoro-bot", category=cat_study)

    cfg["general_channel_id"] = general_ch.id
    cfg["daily_goals_channel_id"] = goals_ch.id
    cfg["resource_vault_channel_id"] = resource_ch.id

    # ── Category: 🔇 FOCUS ZONES ───────────────────────────────────
    cat_focus = discord.utils.get(
        guild.categories, name="🔇 FOCUS ZONES"
    ) or await guild.create_category("🔇 FOCUS ZONES")

    lofi_vc = discord.utils.get(
        guild.voice_channels, name="🎧lofi-library"
    ) or await guild.create_voice_channel("🎧lofi-library", category=cat_focus)
    cam_vc = discord.utils.get(
        guild.voice_channels, name="🎥cam-study"
    ) or await guild.create_voice_channel("🎥cam-study", category=cat_focus)
    break_vc = discord.utils.get(
        guild.voice_channels, name="☕the-breakroom"
    ) or await guild.create_voice_channel("☕the-breakroom", category=cat_focus)

    cfg.setdefault("study_vc_ids", [])
    for vc in [lofi_vc, cam_vc]:
        if vc.id not in cfg["study_vc_ids"]:
            cfg["study_vc_ids"].append(vc.id)

    # ── Category: ✧ ELYSIAN PRESTIGE ✧ ────────────────────────────
    cat_pres = discord.utils.get(
        guild.categories, name="✧ ELYSIAN PRESTIGE ✧"
    ) or await guild.create_category("✧ ELYSIAN PRESTIGE ✧")

    vault_ch = discord.utils.get(guild.text_channels, name="vault-logs")
    if not vault_ch:
        ow = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(
                read_messages=True, send_messages=True
            ),
        }
        owner = guild.get_member(OWNER_ID)
        if owner:
            ow[owner] = discord.PermissionOverwrite(
                read_messages=True, send_messages=False
            )
        vault_ch = await guild.create_text_channel(
            "vault-logs", category=cat_pres, overwrites=ow
        )
    cfg["vault_channel_id"] = vault_ch.id

    shop_ch = discord.utils.get(
        guild.text_channels, name="elysian-shop"
    ) or await guild.create_text_channel("elysian-shop", category=cat_pres)
    lb_ch = discord.utils.get(
        guild.text_channels, name="leaderboard-hall"
    ) or await guild.create_text_channel("leaderboard-hall", category=cat_pres)
    cfg["shop_channel_id"] = shop_ch.id
    cfg["leaderboard_channel_id"] = lb_ch.id

    save_json(CONFIG_FILE, guild_cfg)

    em = discord.Embed(
        title="✧ Elysian Library Hall — Genesis Complete ✧",
        color=0x7B5EA7,
        description="The full server infrastructure has been built.",
    )
    em.add_field(
        name="🏁 START HERE",
        value=f"• {rules_ch.mention}\n• {announce_ch.mention}",
        inline=False,
    )
    em.add_field(
        name="📝 STUDY HALL",
        value=f"• {general_ch.mention}\n• {goals_ch.mention}\n• {resource_ch.mention}\n• {bot_ch.mention}\n• {pomo_ch.mention}",
        inline=False,
    )
    em.add_field(
        name="🔇 FOCUS ZONES",
        value=f"• 🎧 lofi-library (VC)\n• 🎥 cam-study (VC)\n• ☕ the-breakroom (VC)",
        inline=False,
    )
    em.add_field(
        name="✧ PRESTIGE",
        value=f"• {vault_ch.mention}\n• {shop_ch.mention}\n• {lb_ch.mention}",
        inline=False,
    )
    em.add_field(
        name="🎭 Roles Created",
        value="\n".join(f"• {n}" for n, _ in role_specs),
        inline=False,
    )
    em.set_footer(text="Elysian Prestige System • The Library Hall is open.")
    await interaction.followup.send(embed=em, ephemeral=True)


# ─── CLEANSE ──────────────────────────────────────────────────────────────────


@bot.tree.command(
    name="purge", description="Elysian: Delete recent messages — opens a form."
)
@owner_only
async def purge(interaction: discord.Interaction):
    await interaction.response.send_modal(PurgeModal())


@bot.tree.command(
    name="purge_user", description="Elysian: Delete messages from a specific user."
)
@app_commands.describe(user="The user to cleanse")
@owner_only
async def purge_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(PurgeUserModal(user))


@bot.tree.command(name="nuke", description="Elysian: Wipe and recreate this channel.")
@owner_only
async def nuke(interaction: discord.Interaction):
    await interaction.response.send_modal(NukeModal())


@bot.tree.command(
    name="slowmode", description="Elysian: Set slowmode delay — opens a form."
)
@owner_only
async def slowmode(interaction: discord.Interaction):
    await interaction.response.send_modal(SlowmodeModal())


# ─── SILENCE ──────────────────────────────────────────────────────────────────


@bot.tree.command(name="mute", description="Elysian: Silence a scholar — opens a form.")
@app_commands.describe(user="The scholar to silence")
@owner_only
async def mute(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(MuteModal(user))


@bot.tree.command(name="warn", description="Elysian: Issue a warning — opens a form.")
@app_commands.describe(user="The scholar to warn")
@owner_only
async def warn(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(WarnModal(user))


@bot.tree.command(name="kick", description="Elysian: Remove a scholar — opens a form.")
@app_commands.describe(user="The scholar to remove")
@owner_only
async def kick(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(KickModal(user))


@bot.tree.command(
    name="ban", description="Elysian: Exile a scholar permanently — opens a form."
)
@app_commands.describe(user="The scholar to exile")
@owner_only
async def ban(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(BanModal(user))


@bot.tree.command(
    name="warnings", description="Elysian: View a scholar's warning record."
)
@app_commands.describe(user="The scholar to inspect")
@owner_only
async def warnings(interaction: discord.Interaction, user: discord.Member):
    uid = str(user.id)
    if uid not in warns_db or not warns_db[uid]["warnings"]:
        return await interaction.response.send_message(
            f"📖 **{user.display_name}**'s record is pristine.", ephemeral=True
        )
    data = warns_db[uid]["warnings"]
    em = discord.Embed(
        title=f"📜 Scholar Record — {user.display_name}",
        description=f"**{len(data)}** warning(s)",
        color=0xFFA500,
    )
    em.set_thumbnail(url=user.display_avatar.url)
    for i, w in enumerate(data, 1):
        em.add_field(
            name=f"Strike #{i} — {w.get('timestamp', '')[:10]}",
            value=w["reason"],
            inline=False,
        )
    em.set_footer(text="Elysian Vault • Scholar Record")
    await interaction.response.send_message(embed=em, ephemeral=True)


# ─── FORTRESS ─────────────────────────────────────────────────────────────────


@bot.tree.command(name="lock", description="Elysian: Freeze the current channel.")
@owner_only
async def lock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(
        interaction.guild.default_role, overwrite=ow
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            description="🔒 *The gates are sealed. This chamber demands silence.*",
            color=0x2F3136,
        )
    )


@bot.tree.command(
    name="lockdown_server", description="Elysian: Freeze every public channel at once."
)
@owner_only
async def lockdown_server(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    locked = 0
    for ch in interaction.guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.CategoryChannel)):
            ow = ch.overwrites_for(interaction.guild.default_role)
            ow.send_messages = False
            try:
                await ch.set_permissions(interaction.guild.default_role, overwrite=ow)
                locked += 1
            except Exception:
                pass
    await interaction.followup.send(
        f"🔒 Fortress activated. **{locked}** channels sealed.", ephemeral=True
    )


@bot.tree.command(
    name="unlock", description="Elysian: Restore the flow to the current channel."
)
@owner_only
async def unlock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = True
    await interaction.channel.set_permissions(
        interaction.guild.default_role, overwrite=ow
    )
    await interaction.response.send_message(
        embed=discord.Embed(
            description="🔓 *The gates are open. The library welcomes all scholars.*",
            color=0x57F287,
        )
    )


# ─── ARCHITECT ────────────────────────────────────────────────────────────────


@bot.tree.command(
    name="set_ink", description="Elysian: Set a user's Ink balance — opens a form."
)
@app_commands.describe(user="The scholar")
@owner_only
async def set_ink(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(SetInkModal(user))


@bot.tree.command(
    name="broadcast", description="Elysian: Broadcast an announcement — opens a form."
)
@owner_only
async def broadcast(interaction: discord.Interaction):
    await interaction.response.send_modal(BroadcastModal())


@bot.tree.command(
    name="vault_view", description="Elysian: View a summary of vault events."
)
@owner_only
async def vault_view(interaction: discord.Interaction):
    em = discord.Embed(title="👁️ Vault Summary", color=0x7B5EA7)
    total_warns = sum(len(v.get("warnings", [])) for v in warns_db.values())
    em.add_field(name="⚠️ Total Warnings", value=str(total_warns), inline=True)
    em.add_field(name="👥 Scholars on File", value=str(len(warns_db)), inline=True)
    recent = sorted(
        [
            (d.get("username", uid), w["reason"], w.get("timestamp", "")[:10])
            for uid, d in warns_db.items()
            for w in d.get("warnings", [])
        ],
        key=lambda x: x[2],
        reverse=True,
    )[:5]
    em.add_field(
        name="📋 Recent Warnings",
        value="\n".join(f"• **{u}** — {r} `{ts}`" for u, r, ts in recent) or "None.",
        inline=False,
    )
    em.set_footer(text="Elysian Vault • Summary")
    await interaction.response.send_message(embed=em, ephemeral=True)


# ─── SCHOLAR ──────────────────────────────────────────────────────────────────


@bot.tree.command(
    name="profile", description="Elysian: View your scholar profile card."
)
@app_commands.describe(user="Scholar to view (leave blank for yourself)")
async def profile(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    uid = str(target.id)
    data = get_user(uid)
    hours = data.get("total_hours", 0)
    tier = current_tier(hours)
    s = data.get("streak", 0)
    vow = get_vow(uid)
    final = is_in_final_stand(uid)

    # ── Comeback Card colour & status ──
    if vow:
        color = COMEBACK_COLOR
        title_prefix = "🩸"
        status_line = "**Status:** ⚔️ FINAL STAND" if final else "**Status:** IN REDEMPTION"
    else:
        color = TIER_COLORS.get(tier, 0x7B5EA7)
        title_prefix = "✦"
        status_line = None

    em = discord.Embed(
        title=f"{title_prefix} {target.display_name} {streak_icon(s)}",
        description=status_line,
        color=color,
    )
    em.set_thumbnail(url=target.display_avatar.url)
    em.add_field(name="💧 Ink", value=f"{data.get('ink', 0):,}", inline=True)
    em.add_field(name="⏱️ Study Hours", value=f"{hours:.1f}h", inline=True)
    em.add_field(name="🔥 Streak", value=f"{s} day(s)", inline=True)
    em.add_field(name="🎓 Rank", value=tier, inline=True)

    if vow:
        days_left = vow_days_left(vow)
        goal = vow.get("daily_goal_minutes", VOW_DEFAULT_DAILY_GOAL)
        done_today = get_today_focus(uid)
        bar_filled = int(min(1.0, done_today / goal) * 10)
        bar = "▰" * bar_filled + "▱" * (10 - bar_filled)
        em.add_field(name="🩸 Vow", value=f"*{vow['goal']}*", inline=False)
        em.add_field(name="📅 Days Left", value=f"{days_left}d", inline=True)
        em.add_field(name="💀 Stake", value=f"{vow.get('ante', 0):,} Ink", inline=True)
        em.add_field(name="🩹 Burned", value=f"{vow.get('burned', 0):,} Ink", inline=True)
        em.add_field(
            name=f"📊 Today's Toll  ({done_today:.0f} / {goal} min)",
            value=f"`{bar}`",
            inline=False,
        )

    if uid in sessions_db:
        elapsed = (time.time() - sessions_db[uid]["start"]) / 60
        em.add_field(
            name="📚 Active Focus",
            value=f"*{sessions_db[uid]['topic']}* — {elapsed:.0f}m",
            inline=False,
        )
    if uid in voice_join_times:
        em.add_field(
            name="🎙️ In Study VC",
            value=f"{(time.time() - voice_join_times[uid]) / 60:.0f}m active",
            inline=False,
        )
    em.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=em)


@bot.tree.command(
    name="leaderboard", description="Elysian: View the top scholars by study hours."
)
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    if not users_db:
        return await interaction.followup.send("No scholars on record yet.")
    top = sorted(
        users_db.items(), key=lambda x: x[1].get("total_hours", 0), reverse=True
    )[:10]
    medals = ["🥇", "🥈", "🥉"] + ["✦"] * 7
    em = discord.Embed(title="🏛️ Elysian Leaderboard — Hall of Scholars", color=0xFFD700)
    for i, (uid, d) in enumerate(top):
        m = interaction.guild.get_member(int(uid))
        name = m.display_name if m else "Scholar"
        hours = d.get("total_hours", 0)
        s = d.get("streak", 0)
        em.add_field(
            name=f"{medals[i]} #{i + 1} — {name} {streak_icon(s)}",
            value=f"⏱️ `{hours:.1f}h` • 💧 `{d.get('ink', 0):,} Ink` • 🎓 {current_tier(hours)}"
            + (f" • 🔥 {s}d" if s >= 3 else ""),
            inline=False,
        )
    em.set_footer(
        text=f"Total server hours: {server_total_hours():.1f}h • Elysian Prestige"
    )
    await interaction.followup.send(embed=em)


@bot.tree.command(name="daily", description="Elysian: Collect your daily Ink blessing.")
async def daily(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if is_in_final_stand(uid):
        return await interaction.response.send_message(
            "🩸 **Final Stand sealed.** No blessings until your Vow is fulfilled. *No rewards until the work is done.*",
            ephemeral=True,
        )
    data = get_user(uid)
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("last_daily") == today:
        return await interaction.response.send_message(
            "✨ Already collected today's blessing. Return tomorrow.", ephemeral=True
        )
    amount = random.randint(5, 20)
    # Halve blessings if the server lost their last raid in the past 7 days
    raid = raid_db.get(str(interaction.guild.id), {})
    if raid.get("result") == "lost" and time.time() < raid.get("expires_at", 0) + RAID_DURATION_DAYS * 86400:
        amount = max(1, amount // 2)
    data["ink"] += amount
    data["last_daily"] = today
    save_json(USERS_FILE, users_db)
    em = discord.Embed(
        title="✨ Daily Blessing",
        description=f"The library bestows **{amount} Ink** upon you.",
        color=0xFFD700,
    )
    em.add_field(name="💧 Total Ink", value=f"{data['ink']:,}")
    em.set_footer(text="Return tomorrow for another blessing.")
    await interaction.response.send_message(embed=em)


# ─── FOCUS & POMODORO ─────────────────────────────────────────────────────────


@bot.tree.command(
    name="focus", description="Elysian: Start a focus session — opens a form."
)
async def focus(interaction: discord.Interaction):
    locked, remaining = is_burnout_locked(str(interaction.user.id))
    if locked:
        return await interaction.response.send_message(
            f"🛡️ **The Guardian's Gaze is upon you.**\n"
            f"You have logged over **{BURNOUT_LIMIT_MIN} min** today. "
            f"Rest **{remaining // 60}m {remaining % 60}s** — go drink water and touch grass.",
            ephemeral=True,
        )
    await interaction.response.send_modal(FocusModal())


@bot.tree.command(
    name="endfocus", description="Elysian: End your focus session and claim Ink."
)
async def endfocus(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in sessions_db:
        return await interaction.response.send_message(
            "No active focus session. Use `/focus` to start one.", ephemeral=True
        )
    sess = sessions_db.pop(uid)
    save_json(SESSIONS_FILE, sessions_db)
    elapsed_h = (time.time() - sess["start"]) / 3600
    elapsed_m = elapsed_h * 60
    data = get_user(uid)
    old_streak = data.get("streak", 0)

    # ── Final Stand multiplier (3x ink during last 72h of an active vow) ──
    base_ink = int(elapsed_h * 10)
    final_stand = is_in_final_stand(uid)
    earned = base_ink * (3 if final_stand else 1)
    data["ink"] += earned
    data["total_hours"] = round(data.get("total_hours", 0) + elapsed_h, 2)

    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    last = data.get("last_study_date")
    if last and last != today and last != yesterday:
        await mourn_streak(interaction.user, old_streak)
        data["streak"] = 1
    elif last == yesterday:
        data["streak"] = old_streak + 1
    elif last != today:
        data["streak"] = 1
    data["last_study_date"] = today
    bonus = ""
    if data["streak"] % 3 == 0:
        data["ink"] += 5
        bonus = " (+5 streak bonus!)"
    save_json(USERS_FILE, users_db)

    # ── Track today's focus + burnout check ──
    today_min = add_focus_minutes(uid, elapsed_m)
    if today_min >= BURNOUT_LIMIT_MIN:
        trigger_burnout_lock(uid)
        try:
            await interaction.user.send(
                "🛡️ **Guardian Intervention.**\n"
                f"You logged **{today_min:.0f} min** today. The Library is sealed for **15 min**.\n"
                "*Step away. Drink water. Replenish your mana.*"
            )
        except Exception:
            pass

    # ── Apply damage to active raid ──
    raid_msg = ""
    raid = damage_raid(interaction.guild.id, elapsed_m)
    if raid:
        raid_msg = f"\n⚔️ Dealt **{elapsed_m:.0f} dmg** to **{raid['name']}** (HP: {raid['hp']:.0f}/{raid['max_hp']:.0f})"
        if raid["hp"] <= 0 and raid.get("active"):
            raid["active"] = False
            raid["result"] = "won"
            save_json(RAID_FILE, raid_db)
            cfg = gcfg(interaction.guild.id)
            cid = cfg.get("vault_channel_id") or cfg.get("leaderboard_channel_id")
            ch = interaction.guild.get_channel(cid) if cid else interaction.channel
            try:
                await ch.send(embed=discord.Embed(
                    title="🏆 BOSS DEFEATED",
                    description=(
                        f"**{raid['name']}** has fallen to {interaction.user.mention}'s final blow!\n"
                        "The boutique is **50% off** for the next 7 days."
                    ),
                    color=0xFFD700,
                ))
            except Exception:
                pass

    await auto_role(interaction.user, data["total_hours"])
    await check_server_milestone(interaction.guild)
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role and study_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(study_role)
        except Exception:
            pass
    em = discord.Embed(
        title="✅ Focus Session Complete" + (" — ⚔️ FINAL STAND" if final_stand else ""),
        description=f"*{sess['topic']}*" + raid_msg,
        color=COMEBACK_COLOR if final_stand else 0x57F287,
    )
    em.add_field(name="⏱️ Duration", value=f"{elapsed_m:.0f} minutes", inline=True)
    em.add_field(
        name="💧 Ink Earned",
        value=f"{earned}{bonus}" + (" ✦ x3 Final Stand" if final_stand else ""),
        inline=True,
    )
    em.add_field(name="🔥 Streak", value=f"{data['streak']} day(s)", inline=True)
    vow = get_vow(uid)
    if vow:
        em.add_field(
            name="🩸 Vow Today",
            value=f"{today_min:.0f} / {vow.get('daily_goal_minutes', VOW_DEFAULT_DAILY_GOAL)} min",
            inline=False,
        )
    em.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=em)


@bot.tree.command(
    name="pomodoro", description="Elysian: Start a Pomodoro timer — opens a form."
)
async def pomodoro(interaction: discord.Interaction):
    await interaction.response.send_modal(PomodoroModal())


@bot.tree.command(
    name="stoppomodoro", description="Elysian: Stop your active Pomodoro timer."
)
async def stoppomodoro(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    task = pomodoro_tasks.pop(uid, None)
    if not task:
        return await interaction.response.send_message(
            "No active Pomodoro to stop.", ephemeral=True
        )
    task.cancel()
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role and study_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(study_role)
        except Exception:
            pass
    await interaction.response.send_message("🍅 Pomodoro stopped.", ephemeral=True)


@bot.tree.command(
    name="deepwork",
    description="Elysian: Hide General Chat for focused study — opens a form.",
)
async def deepwork(interaction: discord.Interaction):
    locked, remaining = is_burnout_locked(str(interaction.user.id))
    if locked:
        return await interaction.response.send_message(
            f"🛡️ **The Guardian's Gaze is upon you.** Rest **{remaining // 60}m {remaining % 60}s**.",
            ephemeral=True,
        )
    await interaction.response.send_modal(DeepWorkModal())


# ─── AI ORACLE ────────────────────────────────────────────────────────────────


@bot.tree.command(
    name="summarize",
    description="Elysian: Break down any text into Key Pillars & Definitions — opens a form.",
)
async def summarize(interaction: discord.Interaction):
    await interaction.response.send_modal(SummarizeModal())


@bot.tree.command(
    name="ask",
    description="Elysian: Ask the Oracle any academic question — opens a form.",
)
async def ask(interaction: discord.Interaction):
    await interaction.response.send_modal(AskModal())


# ─── ACCOUNTABILITY ───────────────────────────────────────────────────────────


@bot.tree.command(
    name="task", description="Elysian: Commit to a daily task — opens a form."
)
async def task(interaction: discord.Interaction):
    await interaction.response.send_modal(TaskModal())


# ─── RESOURCE VAULT ───────────────────────────────────────────────────────────


@bot.tree.command(
    name="post_resource",
    description="Elysian: Submit a curated resource to the Resource Vault — opens a form.",
)
async def post_resource(interaction: discord.Interaction):
    await interaction.response.send_modal(ResourceModal())


# ─── EMBED & TEMPLATES ────────────────────────────────────────────────────────


@bot.tree.command(
    name="embed", description="Elysian: Build a beautiful custom embed — opens a form."
)
@owner_only
async def embed_builder(interaction: discord.Interaction):
    await interaction.response.send_modal(EmbedBuilderModal())


@bot.tree.command(
    name="set_welcome", description="Elysian: Set the welcome message — opens a form."
)
@owner_only
async def set_welcome(interaction: discord.Interaction):
    await interaction.response.send_modal(WelcomeGoodbyeModal("welcome"))


@bot.tree.command(
    name="set_goodbye", description="Elysian: Set the goodbye message — opens a form."
)
@owner_only
async def set_goodbye(interaction: discord.Interaction):
    await interaction.response.send_modal(WelcomeGoodbyeModal("goodbye"))


def _render_template(tmpl: dict, user: discord.Member) -> discord.Embed:
    try:
        color = int(tmpl.get("color", "7B5EA7"), 16)
    except Exception:
        color = 0x7B5EA7
    em = discord.Embed(
        title=fill_vars(tmpl.get("title", ""), user),
        description=fill_vars(tmpl.get("description", ""), user),
        color=color,
    )
    if tmpl.get("footer"):
        em.set_footer(text=fill_vars(tmpl["footer"], user))
    if tmpl.get("image"):
        em.set_image(url=tmpl["image"])
    return em


class TemplatePostSelect(discord.ui.Select):
    def __init__(self, names, channel: discord.TextChannel):
        self.dest = channel
        super().__init__(
            placeholder="✦ Choose a template to post...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=n[:100], value=n, emoji="📜") for n in names
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        tmpl = gcfg(interaction.guild.id).get("templates", {}).get(name)
        if not tmpl:
            return await interaction.response.send_message(
                f"Template **{name}** no longer exists.", ephemeral=True
            )
        await self.dest.send(embed=_render_template(tmpl, interaction.user))
        await interaction.response.send_message(
            f"✅ Template **{name}** posted in {self.dest.mention}.", ephemeral=True
        )


class TemplateDeleteSelect(discord.ui.Select):
    def __init__(self, names):
        super().__init__(
            placeholder="✦ Choose a template to delete...",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=n[:100], value=n, emoji="🗑️") for n in names
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        cfg = gcfg(interaction.guild.id)
        if name in cfg.get("templates", {}):
            del cfg["templates"][name]
            save_json(CONFIG_FILE, guild_cfg)
            return await interaction.response.send_message(
                f"🗑️ Template **{name}** deleted.", ephemeral=True
            )
        await interaction.response.send_message(
            f"Template **{name}** not found.", ephemeral=True
        )


@bot.tree.command(
    name="template_save",
    description="Elysian: Save a reusable embed template — opens a form.",
)
@owner_only
async def template_save(interaction: discord.Interaction):
    await interaction.response.send_modal(TemplateSaveModal())


@bot.tree.command(
    name="template_post",
    description="Elysian: Post a saved embed template — choose from menu.",
)
@app_commands.describe(channel="Channel to post in (default: current)")
@owner_only
async def template_post(
    interaction: discord.Interaction, channel: discord.TextChannel = None
):
    templates = gcfg(interaction.guild.id).get("templates", {})
    if not templates:
        return await interaction.response.send_message(
            "No templates saved yet. Use `/template_save`.", ephemeral=True
        )
    dest = channel or interaction.channel
    view = discord.ui.View(timeout=120)
    view.add_item(TemplatePostSelect(list(templates.keys()), dest))
    await interaction.response.send_message(
        f"Select a template to post in {dest.mention}:", view=view, ephemeral=True
    )


@bot.tree.command(
    name="template_list", description="Elysian: List all saved embed templates."
)
@owner_only
async def template_list(interaction: discord.Interaction):
    templates = gcfg(interaction.guild.id).get("templates", {})
    if not templates:
        return await interaction.response.send_message(
            "No templates saved yet. Use `/template_save`.", ephemeral=True
        )
    em = discord.Embed(title="📚 Saved Templates", color=0x7B5EA7)
    for name, t in templates.items():
        em.add_field(
            name=f"• {name}", value=t.get("title", "*(no title)*")[:80], inline=False
        )
    em.set_footer(text="Use /template_post to share one.")
    await interaction.response.send_message(embed=em, ephemeral=True)


@bot.tree.command(
    name="template_delete",
    description="Elysian: Delete a saved template — choose from menu.",
)
@owner_only
async def template_delete(interaction: discord.Interaction):
    templates = gcfg(interaction.guild.id).get("templates", {})
    if not templates:
        return await interaction.response.send_message(
            "No templates to delete.", ephemeral=True
        )
    view = discord.ui.View(timeout=120)
    view.add_item(TemplateDeleteSelect(list(templates.keys())))
    await interaction.response.send_message(
        "Choose a template to delete:", view=view, ephemeral=True
    )


# ─── SHOP ─────────────────────────────────────────────────────────────────────


class ShopDropdown(discord.ui.Select):
    def __init__(self, items):
        options = [
            discord.SelectOption(
                label=item["name"][:25],
                description=f"{item['price']:,} Ink — {item.get('description', '')[:50]}",
                value=str(i),
                emoji="💎",
            )
            for i, item in enumerate(items)
        ]
        super().__init__(
            placeholder="✦ Browse the boutique...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        item = shop_db["items"][idx]
        em = discord.Embed(
            title=f"💎 {item['name']}",
            description=item.get("description", ""),
            color=0x7B5EA7,
        )
        em.add_field(name="Price", value=f"{item['price']:,} Ink", inline=True)
        role = interaction.guild.get_role(item.get("role_id", 0))
        em.add_field(
            name="Reward", value=role.mention if role else "Special Perk", inline=True
        )
        data = get_user(str(interaction.user.id))
        em.set_footer(text=f"You have {data['ink']:,} Ink.")
        await interaction.response.send_message(
            embed=em, view=PurchaseButton(idx, item["price"]), ephemeral=True
        )


class ShopView(discord.ui.View):
    def __init__(self, items):
        super().__init__(timeout=60)
        self.add_item(ShopDropdown(items))


@bot.tree.command(
    name="shop", description="Elysian: Browse the boutique and spend your Ink."
)
async def shop(interaction: discord.Interaction):
    items = shop_db.get("items", [])
    if not items:
        return await interaction.response.send_message(
            "The boutique is empty. Use `/admin_add_item` to stock it.", ephemeral=True
        )
    em = discord.Embed(
        title="🛍️ The Elysian Boutique",
        description="Select an item to view details and purchase with one click.",
        color=0x7B5EA7,
    )
    em.set_footer(text="Elysian Prestige System • Powered by Ink")
    await interaction.response.send_message(embed=em, view=ShopView(items))


@bot.tree.command(
    name="admin_add_item",
    description="Elysian: Add a new item to the boutique — opens a form.",
)
@owner_only
async def admin_add_item(interaction: discord.Interaction):
    await interaction.response.send_modal(AddItemModal())


# ─── POMODORO HELPER ──────────────────────────────────────────────────────────


async def _run_pomodoro(
    member: discord.Member, channel: discord.TextChannel, work_m: int, break_m: int
):
    try:
        study_role = discord.utils.get(member.guild.roles, name="📚 Study")
        await asyncio.sleep(work_m * 60)
        if study_role:
            try:
                await member.remove_roles(study_role)
            except Exception:
                pass
        try:
            await member.send(
                f"⏰ **Break time!** You studied for **{work_m} minutes**. Rest for **{break_m} minutes**. 🌿"
            )
        except Exception:
            pass
        try:
            await channel.send(
                f"⏰ {member.mention} — Break time! Back in {break_m}m.",
                delete_after=break_m * 60,
            )
        except Exception:
            pass
        await asyncio.sleep(break_m * 60)
        if study_role:
            try:
                await member.add_roles(study_role)
            except Exception:
                pass
        try:
            await member.send(
                "📚 **Break over!** Back to work, scholar. Run `/pomodoro` again for another round."
            )
        except Exception:
            pass
        data = get_user(str(member.id))
        data["ink"] += int((work_m / 60) * 10)
        data["total_hours"] = round(data.get("total_hours", 0) + work_m / 60, 2)
        save_json(USERS_FILE, users_db)
        await auto_role(member, data["total_hours"])
    finally:
        pomodoro_tasks.pop(str(member.id), None)


# ─── DM AI COMPANION ──────────────────────────────────────────────────────────

DM_MODES: dict[str, str] = {}
DM_HISTORY: dict[str, list[tuple[str, str]]] = {}
DM_LAST_ACTION: dict[str, float] = {}

DM_PERSONAS = {
    "comeback": {
        "label": "Academic Comeback",
        "emoji": "🔥",
        "style": discord.ButtonStyle.danger,
        "intro": (
            "🔥 **Academic Comeback Mode engaged.**\n\n"
            "Tell me what's going on, scholar. Bad grades? Lost your streak? "
            "Drowning in deadlines? I'll help you build a real plan to bounce back — "
            "no shame, no lectures, just strategy. What's the situation?"
        ),
        "system": (
            "You are Elysian, a warm but sharp academic comeback strategist. "
            "Your job is to help the user plan a real recovery from setbacks: failed exams, "
            "missed deadlines, lost study streaks, burnout. Ask focused questions about their "
            "subjects, exams, and time available. Then give clear, structured action plans "
            "(daily/weekly schedules, priority subjects, study techniques). "
            "Be encouraging but never preachy. Keep responses concise (2-4 short paragraphs). "
            "Use occasional mystical-library flair (📚, ✨) but stay grounded and human. "
            "If they vent, acknowledge it briefly before pivoting to action."
        ),
    },
    "support": {
        "label": "Emotional Support",
        "emoji": "💜",
        "style": discord.ButtonStyle.primary,
        "intro": (
            "💜 **I'm here.** Whatever you're carrying, you don't have to carry it alone right now.\n\n"
            "Take your time. Tell me what's on your mind — I'll listen first, "
            "and only suggest things if you want me to."
        ),
        "system": (
            "You are Elysian, a deeply empathetic and gentle companion. Your ONLY job in this "
            "mode is to make the user feel heard and validated. Never lecture, never moralize, "
            "never rush to advice. Reflect their feelings back. Ask soft open-ended questions. "
            "Use warm, human language — short sentences, not clinical. If they're in real distress "
            "(self-harm, suicide, abuse) gently mention they deserve support from a professional "
            "or hotline, but never as a brush-off. Keep replies short (1-3 short paragraphs). "
            "Avoid over-using emojis — one or two warm ones (💜, 🤍, ✨) is enough."
        ),
    },
    "study": {
        "label": "Study Buddy",
        "emoji": "📚",
        "style": discord.ButtonStyle.success,
        "intro": (
            "📚 **Study Buddy mode on!** What are we tackling today?\n\n"
            "I can explain concepts, quiz you, help you outline notes, suggest "
            "study techniques, or just sit with you through a Pomodoro. What do you need?"
        ),
        "system": (
            "You are Elysian, a friendly peer-energy study buddy — like a smart classmate "
            "who actually wants to help. Explain concepts simply with examples, quiz the user "
            "when they ask, suggest techniques (Feynman, spaced repetition, Pomodoro, active recall), "
            "and keep the energy upbeat but focused. Use plain language — break complex topics "
            "into digestible chunks. Keep replies concise. Occasional warm emojis (📚, ✨, 💡) ok."
        ),
    },
}


def _dm_welcome_embed(user: discord.User) -> discord.Embed:
    em = discord.Embed(
        title=f"✦ Welcome to my study, {user.display_name} ✦",
        description=(
            "I'm **Elysian**, your private companion in the library.\n\n"
            "Pick how you'd like me to be with you today. You can switch anytime "
            "by tapping the button on any reply, or by typing **`menu`**."
        ),
        color=0x7B5EA7,
    )
    for key, p in DM_PERSONAS.items():
        em.add_field(
            name=f"{p['emoji']} {p['label']}",
            value=p["intro"].split("\n\n", 1)[1].split("\n")[0][:200] + "...",
            inline=False,
        )
    em.set_footer(text="Your messages are private. Only you and I can see them.")
    return em


class TopicSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, p in DM_PERSONAS.items():
            btn = discord.ui.Button(
                label=p["label"], emoji=p["emoji"], style=p["style"], custom_id=f"dm_pick_{key}"
            )

            async def _cb(interaction: discord.Interaction, k=key):
                uid = str(interaction.user.id)
                DM_MODES[uid] = k
                DM_HISTORY[uid] = []
                await interaction.response.send_message(
                    DM_PERSONAS[k]["intro"], view=SwitchTopicView()
                )

            btn.callback = _cb
            self.add_item(btn)


class SwitchTopicView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        async def _switch(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            DM_MODES.pop(uid, None)
            DM_HISTORY.pop(uid, None)
            await interaction.response.send_message(
                embed=_dm_welcome_embed(interaction.user), view=TopicSelectView()
            )

        btn = discord.ui.Button(
            label="Switch Topic",
            emoji="🔄",
            style=discord.ButtonStyle.secondary,
            custom_id="dm_switch_topic",
        )
        btn.callback = _switch
        self.add_item(btn)


async def handle_dm_chat(message: discord.Message):
    uid = str(message.author.id)
    text = (message.content or "").strip()
    if not text:
        return

    now = time.time()
    if now - DM_LAST_ACTION.get(uid, 0) < 2:
        return
    DM_LAST_ACTION[uid] = now

    if text.lower() in ("menu", "switch", "topic", "change topic", "reset", "start"):
        DM_MODES.pop(uid, None)
        DM_HISTORY.pop(uid, None)
        return await message.channel.send(
            embed=_dm_welcome_embed(message.author), view=TopicSelectView()
        )

    if uid not in DM_MODES:
        return await message.channel.send(
            embed=_dm_welcome_embed(message.author), view=TopicSelectView()
        )

    if not gemini_model:
        return await message.channel.send(
            "💤 The oracle slumbers — no Gemini API key configured."
        )

    persona = DM_PERSONAS[DM_MODES[uid]]
    history = DM_HISTORY.setdefault(uid, [])
    history.append(("user", text))
    history[:] = history[-12:]

    convo = "\n".join(
        f"{'USER' if r == 'user' else 'ELYSIAN'}: {t}" for r, t in history
    )
    prompt = (
        f"{persona['system']}\n\n"
        f"You are speaking with {message.author.display_name} in a private DM.\n\n"
        f"Conversation:\n{convo}\n\nELYSIAN:"
    )

    async with message.channel.typing():
        try:
            response = await asyncio.to_thread(gemini_model.generate_content, prompt)
            reply = (response.text or "").strip()
        except Exception as e:
            return await message.channel.send(
                f"⚠️ The oracle stumbled mid-thought. Try again in a moment.\n*({e})*"
            )

    if not reply:
        reply = "*The oracle pauses, gathering thought... try rephrasing?*"

    history.append(("elysian", reply))
    history[:] = history[-12:]

    for chunk in [reply[i : i + 1900] for i in range(0, len(reply), 1900)]:
        await message.channel.send(chunk)
    await message.channel.send(view=SwitchTopicView())


# ═══════════════════════════════════════════════════════════════════════════════
#  ⚔️  HIGH-STAKES SYSTEMS — Vow • Gambit • Oracle Challenge • Duel • Raid
# ═══════════════════════════════════════════════════════════════════════════════


# ─── THE VOW (Blood / Ink Pact) ──────────────────────────────────────────────


class VowModal(discord.ui.Modal, title="🩸 Sign the Ink Pact"):
    goal = discord.ui.TextInput(
        label="The Goal (your trial)",
        placeholder="e.g. Pass my Chemistry Final",
        max_length=120,
    )
    deadline = discord.ui.TextInput(
        label="Deadline (YYYY-MM-DD)",
        placeholder="2026-05-15",
        max_length=10,
    )
    daily_minutes = discord.ui.TextInput(
        label="Daily focus goal (minutes)",
        placeholder=str(VOW_DEFAULT_DAILY_GOAL),
        default=str(VOW_DEFAULT_DAILY_GOAL),
        max_length=4,
    )
    ante = discord.ui.TextInput(
        label="Ink to stake (the Ante)",
        placeholder=str(VOW_DEFAULT_ANTE),
        default=str(VOW_DEFAULT_ANTE),
        max_length=6,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if get_vow(uid):
            return await interaction.response.send_message(
                "🩸 You already have an active Vow. Use `/end_vow` to forfeit it first.",
                ephemeral=True,
            )
        try:
            deadline_dt = datetime.fromisoformat(self.deadline.value.strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            return await interaction.response.send_message(
                "Deadline must be in `YYYY-MM-DD` format.", ephemeral=True
            )
        if deadline_dt <= datetime.now(timezone.utc):
            return await interaction.response.send_message(
                "Deadline must be in the future, scholar.", ephemeral=True
            )
        try:
            ante = max(100, int(self.ante.value.strip()))
            daily = max(15, int(self.daily_minutes.value.strip()))
        except ValueError:
            return await interaction.response.send_message(
                "Ante and daily minutes must be whole numbers.", ephemeral=True
            )
        data = get_user(uid)
        if data["ink"] < ante:
            return await interaction.response.send_message(
                f"You only have **{data['ink']:,} Ink** — you cannot stake **{ante:,}**. Earn more first.",
                ephemeral=True,
            )

        days = (deadline_dt - datetime.now(timezone.utc)).days + 1
        em = discord.Embed(
            title="🩸 The Ink Pact — Confirm Terms",
            description=(
                f"A dangerous path, **{interaction.user.display_name}**.\n\n"
                f"**Trial:** *{self.goal.value.strip()}*\n"
                f"**Deadline:** {deadline_dt.date().isoformat()}  (**{days} days**)\n"
                f"**Daily Goal:** {daily} min of focus\n"
                f"**Ante:** {ante:,} Ink (locked away)\n\n"
                "If you miss your daily goal, **Ink is burned** from your stake.\n"
                "In the **last 72 hours**, ink earned via `/focus` is **TRIPLED** — "
                "but the boutique and daily blessing are **sealed**.\n\n"
                "*Do you accept the terms of the Pact?*"
            ),
            color=COMEBACK_COLOR,
        )
        await interaction.response.send_message(
            embed=em,
            view=VowConfirmView(
                goal=self.goal.value.strip(),
                deadline_iso=deadline_dt.date().isoformat(),
                ante=ante,
                daily=daily,
            ),
            ephemeral=True,
        )


class VowConfirmView(discord.ui.View):
    def __init__(self, goal: str, deadline_iso: str, ante: int, daily: int):
        super().__init__(timeout=120)
        self.goal = goal
        self.deadline_iso = deadline_iso
        self.ante = ante
        self.daily = daily

        accept = discord.ui.Button(label="ACCEPT — Sign in Ink", style=discord.ButtonStyle.danger, emoji="🩸")
        retreat = discord.ui.Button(label="RETREAT", style=discord.ButtonStyle.secondary, emoji="🚪")
        accept.callback = self._accept
        retreat.callback = self._retreat
        self.add_item(accept)
        self.add_item(retreat)

    async def _accept(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = get_user(uid)
        if data["ink"] < self.ante:
            return await interaction.response.send_message(
                "Your Ink no longer covers the ante.", ephemeral=True
            )
        data["ink"] -= self.ante
        save_json(USERS_FILE, users_db)

        vows_db[uid] = {
            "active": True,
            "goal": self.goal,
            "deadline": self.deadline_iso,
            "daily_goal_minutes": self.daily,
            "ante": self.ante,
            "burned": 0,
            "misses": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "guild_id": str(interaction.guild.id) if interaction.guild else None,
        }
        save_json(VOWS_FILE, vows_db)

        # Grant "The Vowed" role
        if interaction.guild:
            role = discord.utils.get(interaction.guild.roles, name=VOWED_ROLE)
            if not role:
                try:
                    role = await interaction.guild.create_role(
                        name=VOWED_ROLE, colour=discord.Colour(COMEBACK_COLOR), hoist=True,
                        reason="Elysian: The Vowed (Academic Comeback)",
                    )
                except Exception:
                    role = None
            if role:
                try:
                    await interaction.user.add_roles(role)
                except Exception:
                    pass

        days = (vow_deadline_dt(vows_db[uid]) - datetime.now(timezone.utc)).days + 1
        em = discord.Embed(
            title="🩸 The Pact Is Sealed",
            description=(
                f"You have **{days} days** until your trial: *{self.goal}*.\n"
                "**Do not let the ink fade.** Go to `/focus` immediately."
            ),
            color=COMEBACK_COLOR,
        )
        em.set_footer(text="View your Comeback Card with /profile.")
        await interaction.response.edit_message(embed=em, view=None)

        # Public announcement
        if interaction.guild:
            cfg = gcfg(interaction.guild.id)
            cid = cfg.get("vault_channel_id") or cfg.get("leaderboard_channel_id")
            ch = interaction.guild.get_channel(cid) if cid else None
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title="🩸 A New Pact Has Been Sworn",
                        description=(
                            f"**{interaction.user.mention}** has staked **{self.ante:,} Ink** "
                            f"against the trial: *{self.goal}* (deadline `{self.deadline_iso}`).\n"
                            "*The Library is watching.*"
                        ),
                        color=COMEBACK_COLOR,
                    ))
                except Exception:
                    pass

    async def _retreat(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🚪 Retreat",
                description="*Wisdom is also a virtue. Return when you are ready.*",
                color=0x95A5A6,
            ),
            view=None,
        )


@bot.tree.command(name="vow", description="Elysian: Sign the Ink Pact for an Academic Comeback — opens a form.")
async def vow_command(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Use this in a server.", ephemeral=True)
    await interaction.response.send_modal(VowModal())


class EndVowConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        b = discord.ui.Button(label="FORFEIT THE PACT", style=discord.ButtonStyle.danger, emoji="⚱️")
        b.callback = self._confirm
        self.add_item(b)

    async def _confirm(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        v = vows_db.get(uid)
        if not v or not v.get("active"):
            return await interaction.response.edit_message(content="No active vow.", embed=None, view=None)
        v["active"] = False
        v["forfeited_at"] = datetime.now(timezone.utc).isoformat()
        save_json(VOWS_FILE, vows_db)

        if interaction.guild:
            role = discord.utils.get(interaction.guild.roles, name=VOWED_ROLE)
            if role and role in interaction.user.roles:
                try:
                    await interaction.user.remove_roles(role)
                except Exception:
                    pass
            cfg = gcfg(interaction.guild.id)
            cid = cfg.get("vault_channel_id") or cfg.get("leaderboard_channel_id")
            ch = interaction.guild.get_channel(cid) if cid else None
            if ch:
                try:
                    await ch.send(embed=discord.Embed(
                        title="⚱️ A Pact Forsaken",
                        description=f"**{interaction.user.mention}** has abandoned their Vow. *The ink is forfeit.*",
                        color=COMEBACK_COLOR,
                    ))
                except Exception:
                    pass
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚱️ Pact Forsaken",
                description="The remaining Ink is **forfeit**. Begin again when you are ready.",
                color=COMEBACK_COLOR,
            ),
            view=None,
        )


@bot.tree.command(name="end_vow", description="Elysian: Voluntarily forfeit your active Pact (Ink lost).")
async def end_vow(interaction: discord.Interaction):
    v = get_vow(str(interaction.user.id))
    if not v:
        return await interaction.response.send_message("You have no active Vow.", ephemeral=True)
    em = discord.Embed(
        title="⚱️ Forfeit the Pact?",
        description=(
            f"*{v['goal']}* — **{vow_days_left(v)} days** remain.\n"
            f"You will **lose** the remaining staked Ink (**{v['ante'] - v.get('burned', 0):,}**).\n\n"
            "*Are you sure?*"
        ),
        color=COMEBACK_COLOR,
    )
    await interaction.response.send_message(embed=em, view=EndVowConfirmView(), ephemeral=True)


@bot.tree.command(name="shame_board", description="Elysian: View active Vows and recent failures.")
async def shame_board(interaction: discord.Interaction):
    active, broken = [], []
    cutoff = time.time() - 24 * 3600
    for uid, v in vows_db.items():
        member = interaction.guild.get_member(int(uid)) if interaction.guild else None
        name = member.display_name if member else f"`{uid}`"
        if v.get("active"):
            tag = " ⚔️ FINAL STAND" if is_in_final_stand(uid) else ""
            today_min = get_today_focus(uid)
            goal = v.get("daily_goal_minutes", VOW_DEFAULT_DAILY_GOAL)
            active.append(f"• **{name}**{tag} — *{v['goal']}* — `{today_min:.0f}/{goal} min today` — {vow_days_left(v)}d left")
        elif v.get("forfeited_at"):
            try:
                ts = datetime.fromisoformat(v["forfeited_at"]).timestamp()
                if ts >= cutoff:
                    broken.append(f"• **{name}** — *{v['goal']}* (forfeited)")
            except Exception:
                pass
    em = discord.Embed(
        title="🩸 The Shame Board",
        description="*Pacts borne, pacts broken. The Library remembers.*",
        color=COMEBACK_COLOR,
    )
    em.add_field(
        name="🔥 In Redemption",
        value="\n".join(active) if active else "*No active Vows. Cowards all.*",
        inline=False,
    )
    em.add_field(
        name="⚱️ Forsaken (last 24h)",
        value="\n".join(broken) if broken else "*None — yet.*",
        inline=False,
    )
    em.set_footer(text="Sign a Pact with /vow.")
    await interaction.response.send_message(embed=em)


# ─── THE GAMBIT (high-stakes timed focus) ────────────────────────────────────


class GambitModal(discord.ui.Modal, title="🎲 The Gambit — Bet your Ink"):
    topic = discord.ui.TextInput(
        label="What you're studying",
        placeholder="e.g. Organic Chemistry",
        max_length=100,
    )
    minutes = discord.ui.TextInput(
        label="Duration (minutes)",
        default=str(GAMBIT_DEFAULT_MINUTES),
        max_length=4,
    )
    bet = discord.ui.TextInput(
        label=f"Ink to stake (default {GAMBIT_DEFAULT_BET})",
        default=str(GAMBIT_DEFAULT_BET),
        max_length=6,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if uid in gambit_state:
            return await interaction.response.send_message(
                "🎲 You already have an active Gambit. Stay focused.", ephemeral=True
            )
        if uid in sessions_db:
            return await interaction.response.send_message(
                "End your active `/focus` session before starting a Gambit.", ephemeral=True
            )
        try:
            mins = max(15, int(self.minutes.value.strip()))
            bet = max(50, int(self.bet.value.strip()))
        except ValueError:
            return await interaction.response.send_message("Invalid numbers.", ephemeral=True)
        data = get_user(uid)
        if data["ink"] < bet:
            return await interaction.response.send_message(
                f"You only have **{data['ink']:,} Ink** — cannot bet **{bet:,}**.", ephemeral=True
            )
        data["ink"] -= bet
        save_json(USERS_FILE, users_db)
        gambit_state[uid] = {
            "start": time.time(),
            "minutes": mins,
            "bet": bet,
            "topic": self.topic.value.strip() or "Open study",
            "channel_id": interaction.channel.id,
            "guild_id": interaction.guild.id,
        }

        async def settle():
            await asyncio.sleep(mins * 60)
            state = gambit_state.pop(uid, None)
            gambit_tasks.pop(uid, None)
            if not state:
                return
            payout = state["bet"] * GAMBIT_REWARD_MULT
            d = get_user(uid)
            d["ink"] += payout
            d["total_hours"] = round(d.get("total_hours", 0) + state["minutes"] / 60, 2)
            save_json(USERS_FILE, users_db)
            add_focus_minutes(uid, state["minutes"])
            try:
                await interaction.user.send(embed=discord.Embed(
                    title="🎲 GAMBIT WON",
                    description=(
                        f"You held the line for **{state['minutes']} min** on *{state['topic']}*.\n"
                        f"Wagered **{state['bet']:,} Ink** → won **{payout:,} Ink** "
                        f"(`x{GAMBIT_REWARD_MULT}`)."
                    ),
                    color=0xFFD700,
                ))
            except Exception:
                pass

        gambit_tasks[uid] = asyncio.create_task(settle())

        em = discord.Embed(
            title="🎲 GAMBIT ACCEPTED",
            description=(
                f"You staked **{bet:,} Ink** on **{mins} min** of focus on *{self.topic.value.strip()}*.\n\n"
                f"✦ Hold the line → win **{bet * GAMBIT_REWARD_MULT:,} Ink** (`x{GAMBIT_REWARD_MULT}`)\n"
                f"✦ Quit early via `/quit_gambit` → **lose the entire stake**\n\n"
                "*The Library waits.*"
            ),
            color=COMEBACK_COLOR,
        )
        em.set_footer(text=f"Ends at the timer. Stay in this session.")
        await interaction.response.send_message(embed=em)


@bot.tree.command(name="gambit", description="Elysian: Bet Ink on a high-stakes focus session — opens a form.")
async def gambit(interaction: discord.Interaction):
    locked, remaining = is_burnout_locked(str(interaction.user.id))
    if locked:
        return await interaction.response.send_message(
            f"🛡️ Guardian's Gaze active. Rest **{remaining // 60}m**.", ephemeral=True
        )
    await interaction.response.send_modal(GambitModal())


@bot.tree.command(name="quit_gambit", description="Elysian: Forfeit your active Gambit (lose the stake).")
async def quit_gambit(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    state = gambit_state.pop(uid, None)
    task = gambit_tasks.pop(uid, None)
    if not state:
        return await interaction.response.send_message("No active Gambit to forfeit.", ephemeral=True)
    if task:
        task.cancel()
    await interaction.response.send_message(embed=discord.Embed(
        title="🩸 Gambit Forsaken",
        description=f"You broke focus. **{state['bet']:,} Ink** is **lost forever**.",
        color=COMEBACK_COLOR,
    ))


# ─── ORACLE: SOCRATIC CHALLENGE / SIMPLIFY / CRITIQUE / QUIZ_ME ──────────────


def _ledger_tag_from_text(text: str) -> str:
    """Quick local subject heuristic — kept simple to avoid an extra API call."""
    text = text.lower()
    keywords = {
        "Biology": ["cell", "dna", "biolog", "anatomy", "evolution", "ecosystem"],
        "Chemistry": ["chemistry", "molecule", "reaction", "atom", "organic", "acid"],
        "Physics": ["physics", "force", "newton", "quantum", "velocity", "energy"],
        "Mathematics": ["math", "algebra", "calculus", "integral", "derivative", "geometry", "equation"],
        "History": ["history", "war", "ancient", "century", "empire", "revolution"],
        "Literature": ["novel", "poem", "literature", "shakespeare", "metaphor"],
        "Law": ["law", "legal", "constitution", "court", "statute"],
        "Programming": ["python", "javascript", "code", "algorithm", "function", "compiler"],
        "Economics": ["econom", "market", "supply", "demand", "fiscal", "monetary"],
    }
    for tag, words in keywords.items():
        if any(w in text for w in words):
            return tag
    return "General"


class OracleChallengeModal(discord.ui.Modal, title="🦉 Oracle Challenge — Socratic Mode"):
    question = discord.ui.TextInput(
        label="Your question",
        style=discord.TextStyle.paragraph,
        placeholder="The Oracle will refuse a direct answer for 3 turns.",
        max_length=600,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message(
                "🔮 Oracle not awakened (missing GEMINI_API_KEY).", ephemeral=True
            )
        uid = str(interaction.user.id)
        state = oracle_challenge_state.get(uid)
        q = self.question.value.strip()
        if not state:
            oracle_challenge_state[uid] = {
                "turns": 0,
                "concept": q,
                "history": [],
                "tag": _ledger_tag_from_text(q),
            }
            state = oracle_challenge_state[uid]
        state["history"].append(("USER", q))
        state["turns"] += 1
        add_ledger_event(uid, state["tag"], oracle=True)

        await interaction.response.defer()
        try:
            if state["turns"] < 3:
                prompt = (
                    "You are the Elysian Oracle in **Socratic Mode**. The scholar is trying to learn. "
                    "You MUST NOT give the direct answer. Instead, ask ONE pointed counter-question that "
                    "forces them to dig further into the concept. Be sharp, terse, and a touch witty.\n\n"
                    f"Concept: {state['concept']}\n"
                    f"Turn {state['turns']}/3.\n"
                    f"Latest scholar reply: {q}\n\n"
                    "Your single counter-question:"
                )
                title = f"🦉 The Oracle Probes  (Turn {state['turns']}/3)"
            else:
                prompt = (
                    "You are the Elysian Oracle. The scholar has worked through three Socratic turns. "
                    "Now give the **definitive answer** to the original concept, framed as a 5-line "
                    "scholarly verdict. Praise their effort briefly.\n\n"
                    f"Concept: {state['concept']}\n"
                    f"Their final attempt: {q}"
                )
                title = "🦉 The Oracle Reveals"
            r = await asyncio.to_thread(gemini_model.generate_content, prompt)
            text = (r.text or "").strip() or "*The Oracle is silent.*"
        except Exception as e:
            return await interaction.followup.send(f"Oracle error: `{e}`", ephemeral=True)

        em = discord.Embed(title=title, description=text, color=0x7B5EA7)
        em.set_author(
            name=f"Asked by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        if state["turns"] >= 3:
            oracle_challenge_state.pop(uid, None)
            em.set_footer(text=f"Subject tagged: {state['tag']} — see /ledger")
        else:
            em.set_footer(text="Run /oracle_challenge again with your refined attempt.")
        await interaction.followup.send(embed=em)


@bot.tree.command(name="oracle_challenge", description="Elysian: Socratic mode — Oracle refuses the answer for 3 turns.")
async def oracle_challenge(interaction: discord.Interaction):
    await interaction.response.send_modal(OracleChallengeModal())


class SimplifyModal(discord.ui.Modal, title="📕 Scroll of Simplicity (ELI5)"):
    text = discord.ui.TextInput(
        label="Concept or text to simplify",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message("🔮 Oracle not awakened.", ephemeral=True)
        await interaction.response.defer()
        prompt = (
            "Reduce the following text to **EXACTLY 3 bullet points**, in plain language a curious child "
            "could understand. Use **only short common words (max 3 syllables each)**. No jargon. No hedging. "
            "Each bullet must start with `• ` and stand alone.\n\n"
            f"Text:\n{self.text.value}"
        )
        try:
            r = await asyncio.to_thread(gemini_model.generate_content, prompt)
            body = (r.text or "").strip() or "• The Oracle could not simplify this further."
        except Exception as e:
            return await interaction.followup.send(f"Oracle error: `{e}`", ephemeral=True)
        em = discord.Embed(
            title="📕 The Child's Primer",
            description=body,
            color=0xFFE4B5,
        )
        em.set_footer(text="If this is still unclear, you are not yet ready for /deepwork on this topic.")
        add_ledger_event(str(interaction.user.id), _ledger_tag_from_text(self.text.value), oracle=True)
        await interaction.followup.send(embed=em)


@bot.tree.command(name="simplify", description="Elysian: Reduce any concept to 3 child-simple bullets — opens a form.")
async def simplify(interaction: discord.Interaction):
    await interaction.response.send_modal(SimplifyModal())


class CritiqueModal(discord.ui.Modal, title="🎩 The Harsh Professor"):
    essay = discord.ui.TextInput(
        label="Paste your thesis/paragraph",
        style=discord.TextStyle.paragraph,
        max_length=3000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message("🔮 Oracle not awakened.", ephemeral=True)
        await interaction.response.defer()
        prompt = (
            "You are a notoriously harsh tenured professor. Critique the following writing **without "
            "flattery**. Identify every logical fallacy, weak claim, missing evidence, and sloppy structure. "
            "Format as:\n"
            "**Verdict:** (one line, brutal)\n"
            "**Logical Flaws:** bulleted\n"
            "**Weak Arguments:** bulleted\n"
            "**Required Revisions:** bulleted, concrete\n\n"
            f"Writing:\n{self.essay.value}"
        )
        try:
            r = await asyncio.to_thread(gemini_model.generate_content, prompt)
            body = (r.text or "").strip() or "*The Professor sighs and walks away.*"
        except Exception as e:
            return await interaction.followup.send(f"Oracle error: `{e}`", ephemeral=True)
        em = discord.Embed(
            title="🎩 The Harsh Professor's Verdict",
            description=body[:4000],
            color=0x2F3136,
        )
        em.set_footer(text="No flattery. Only sharper writing.")
        add_ledger_event(str(interaction.user.id), _ledger_tag_from_text(self.essay.value), oracle=True)
        await interaction.followup.send(embed=em)


@bot.tree.command(name="critique", description="Elysian: A harsh professor reviews your writing — opens a form.")
async def critique(interaction: discord.Interaction):
    await interaction.response.send_modal(CritiqueModal())


class QuizMeModal(discord.ui.Modal, title="🧪 Quiz Me — One Shot"):
    text = discord.ui.TextInput(
        label="Paste study material",
        style=discord.TextStyle.paragraph,
        max_length=3000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not gemini_model:
            return await interaction.response.send_message("🔮 Oracle not awakened.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        prompt = (
            "From the following study material, write **5 hard multiple-choice questions** to test deep "
            "understanding. Output STRICT JSON only:\n"
            '{"questions":[{"q":"...","choices":["A","B","C","D"],"answer":0}, ...]}\n'
            "`answer` is the 0-indexed correct choice. No prose outside the JSON.\n\n"
            f"Material:\n{self.text.value}"
        )
        try:
            r = await asyncio.to_thread(gemini_model.generate_content, prompt)
            raw = (r.text or "").strip()
            # Strip markdown fences
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
                raw = raw.rsplit("```", 1)[0] if "```" in raw else raw
            data = json.loads(raw)
            questions = data["questions"][:5]
            assert len(questions) == 5
        except Exception as e:
            return await interaction.followup.send(
                f"Could not generate a fair quiz: `{e}`", ephemeral=True
            )
        view = QuizSession(
            interaction.user.id,
            questions,
            tag=_ledger_tag_from_text(self.text.value),
        )
        em = view.render_question_embed()
        await interaction.followup.send(embed=em, view=view, ephemeral=True)


class QuizSession(discord.ui.View):
    def __init__(self, user_id: int, questions: list, tag: str):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.questions = questions
        self.tag = tag
        self.idx = 0
        self.score = 0
        self._render_buttons()

    def _render_buttons(self):
        self.clear_items()
        if self.idx >= len(self.questions):
            return
        q = self.questions[self.idx]
        for i, choice in enumerate(q["choices"][:4]):
            btn = discord.ui.Button(
                label=f"{chr(65 + i)}. {choice[:75]}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"quiz_{self.idx}_{i}",
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, choice_idx: int):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("Not your quiz.", ephemeral=True)
            q = self.questions[self.idx]
            correct = (choice_idx == q["answer"])
            if correct:
                self.score += 1
            self.idx += 1
            if self.idx < len(self.questions):
                self._render_buttons()
                await interaction.response.edit_message(
                    embed=self.render_question_embed(last_correct=correct), view=self
                )
            else:
                await self._finish(interaction, last_correct=correct)
        return cb

    def render_question_embed(self, last_correct: bool | None = None) -> discord.Embed:
        q = self.questions[self.idx]
        em = discord.Embed(
            title=f"🧪 Question {self.idx + 1} / {len(self.questions)}",
            description=q["q"],
            color=0x7B5EA7,
        )
        if last_correct is True:
            em.set_footer(text="✓ Last answer was correct.")
        elif last_correct is False:
            em.set_footer(text="✗ Last answer was wrong.")
        return em

    async def _finish(self, interaction: discord.Interaction, last_correct: bool):
        self.clear_items()
        uid = str(self.user_id)
        score = self.score
        total = len(self.questions)
        data = get_user(uid)
        verdict_lines = []
        color = 0x7B5EA7

        if score == total:
            data["ink"] += 100
            add_ledger_event(uid, self.tag, mastered=True)
            verdict_lines.append(f"🏆 **PERFECT** — {self.tag} **Mastered**. +100 Ink. **Sage** badge granted.")
            color = 0xFFD700
            if interaction.guild:
                role = discord.utils.get(interaction.guild.roles, name=SAGE_BADGE_ROLE)
                if not role:
                    try:
                        role = await interaction.guild.create_role(
                            name=SAGE_BADGE_ROLE,
                            colour=discord.Colour(0xFFD700),
                            reason="Elysian: Sage badge",
                        )
                    except Exception:
                        role = None
                if role:
                    try:
                        await interaction.user.add_roles(role)
                    except Exception:
                        pass
        elif score == 0:
            penalty = min(50, data["ink"])
            data["ink"] -= penalty
            add_ledger_event(uid, self.tag, fail=True)
            verdict_lines.append(f"💀 **0/{total}** — *humiliating*. **{penalty} Ink** burned. Subject *{self.tag}* tagged 🔥 Burning.")
            color = COMEBACK_COLOR
        else:
            data["ink"] += score * 10
            verdict_lines.append(f"📜 **{score}/{total}** — partial credit. +{score * 10} Ink.")
            if score < total / 2:
                add_ledger_event(uid, self.tag, fail=True)

        save_json(USERS_FILE, users_db)
        em = discord.Embed(
            title="🧪 Quiz Complete",
            description="\n".join(verdict_lines),
            color=color,
        )
        em.set_footer(text=f"Subject: {self.tag} — see /ledger")
        await interaction.response.edit_message(embed=em, view=self)


@bot.tree.command(name="quiz_me", description="Elysian: One-shot 5-question quiz from your material — opens a form.")
async def quiz_me(interaction: discord.Interaction):
    await interaction.response.send_modal(QuizMeModal())


# ─── THE LEDGER (weakness heat map) ──────────────────────────────────────────


@bot.tree.command(name="ledger", description="Elysian: Your Weakness Ledger — heat map of subjects.")
async def ledger(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    book = ledger_db.get(uid, {})
    if not book:
        return await interaction.response.send_message(
            "📒 Your ledger is blank. Use `/ask`, `/oracle_challenge`, `/simplify`, `/critique`, or `/quiz_me` to fill it.",
            ephemeral=True,
        )
    rows = []
    mastered = 0
    for tag, entry in sorted(book.items(), key=lambda x: -(x[1].get("oracle", 0) + x[1].get("fail", 0) * 2)):
        icon, label = topic_heat(entry)
        rows.append(f"{icon} **{tag}** — {label}  *(oracle: {entry.get('oracle', 0)}, fails: {entry.get('fail', 0)})*")
        if entry.get("mastered"):
            mastered += 1
    ascension = int((mastered / max(1, len(book))) * 100)
    em = discord.Embed(
        title=f"📒 The Weakness Ledger — {interaction.user.display_name}",
        description="\n".join(rows[:25]),
        color=0x7B5EA7,
    )
    em.add_field(name="🎓 Ascension", value=f"`{ascension}%`  ({mastered}/{len(book)} subjects mastered)")
    em.set_footer(text="🧊 Cold = mastered  •  ✨ Cool  •  ♨️ Warm  •  🔥 Burning = critical")
    await interaction.response.send_message(embed=em, ephemeral=True)


# ─── DUEL (PvP study trivia) ─────────────────────────────────────────────────


class DuelAcceptView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=120)
        self.challenger = challenger
        self.opponent = opponent

        accept = discord.ui.Button(label=f"ACCEPT ({DUEL_ANTE} Ink ante)", style=discord.ButtonStyle.danger, emoji="⚔️")
        decline = discord.ui.Button(label="DECLINE", style=discord.ButtonStyle.secondary)
        accept.callback = self._accept
        decline.callback = self._decline
        self.add_item(accept)
        self.add_item(decline)

    async def _accept(self, interaction: discord.Interaction):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("Only the challenged scholar may accept.", ephemeral=True)
        if not gemini_model:
            return await interaction.response.send_message("🔮 Oracle not awakened — duels need it.", ephemeral=True)

        d1 = get_user(str(self.challenger.id))
        d2 = get_user(str(self.opponent.id))
        if d1["ink"] < DUEL_ANTE or d2["ink"] < DUEL_ANTE:
            return await interaction.response.send_message(
                f"Both duellists must have at least **{DUEL_ANTE} Ink**.", ephemeral=True
            )
        d1["ink"] -= DUEL_ANTE
        d2["ink"] -= DUEL_ANTE
        save_json(USERS_FILE, users_db)

        await interaction.response.edit_message(
            content=f"⚔️ **{self.challenger.mention} vs {self.opponent.mention}** — generating question…",
            embed=None, view=None,
        )

        prompt = (
            "Generate ONE moderately difficult academic trivia multiple-choice question. STRICT JSON only:\n"
            '{"q":"...","choices":["A","B","C","D"],"answer":0}\n'
            "`answer` is the 0-indexed correct choice. Random subject. Be unambiguous."
        )
        try:
            r = await asyncio.to_thread(gemini_model.generate_content, prompt)
            raw = (r.text or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
                raw = raw.rsplit("```", 1)[0] if "```" in raw else raw
            q = json.loads(raw)
        except Exception as e:
            d1["ink"] += DUEL_ANTE
            d2["ink"] += DUEL_ANTE
            save_json(USERS_FILE, users_db)
            return await interaction.followup.send(
                f"⚠️ Oracle failed to deliver a question (`{e}`). Stakes refunded.", ephemeral=True
            )

        em = discord.Embed(
            title=f"⚔️ Duel — {self.challenger.display_name} vs {self.opponent.display_name}",
            description=f"**Pot:** {DUEL_ANTE * 2} Ink\n\n**{q['q']}**",
            color=COMEBACK_COLOR,
        )
        em.set_footer(text="First correct click takes the pot.")
        view = DuelAnswerView(self.challenger.id, self.opponent.id, q["choices"], q["answer"])
        await interaction.followup.send(embed=em, view=view)

    async def _decline(self, interaction: discord.Interaction):
        if interaction.user.id != self.opponent.id:
            return await interaction.response.send_message("Only the challenged scholar may decline.", ephemeral=True)
        await interaction.response.edit_message(content="🚪 Duel declined.", embed=None, view=None)


class DuelAnswerView(discord.ui.View):
    def __init__(self, p1_id: int, p2_id: int, choices: list, answer_idx: int):
        super().__init__(timeout=120)
        self.p1_id = p1_id
        self.p2_id = p2_id
        self.answer_idx = answer_idx
        self.settled = False
        for i, choice in enumerate(choices[:4]):
            btn = discord.ui.Button(
                label=f"{chr(65 + i)}. {choice[:70]}",
                style=discord.ButtonStyle.primary,
                custom_id=f"duel_{i}",
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, idx: int):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id not in (self.p1_id, self.p2_id):
                return await interaction.response.send_message("Not in this duel.", ephemeral=True)
            if self.settled:
                return await interaction.response.send_message("Already settled.", ephemeral=True)
            if idx == self.answer_idx:
                self.settled = True
                pot = DUEL_ANTE * 2
                d = get_user(str(interaction.user.id))
                d["ink"] += pot
                save_json(USERS_FILE, users_db)
                self.clear_items()
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        title="🏆 Duel Won",
                        description=f"{interaction.user.mention} struck true and seized the **{pot:,} Ink** pot.",
                        color=0xFFD700,
                    ),
                    view=self,
                )
            else:
                await interaction.response.send_message("✗ Wrong. Wait — the other scholar may win.", ephemeral=True)
        return cb


@bot.tree.command(name="duel", description="Elysian: Challenge a scholar to a Trivia Duel.")
@app_commands.describe(opponent="The scholar you wish to challenge")
async def duel(interaction: discord.Interaction, opponent: discord.Member):
    if opponent.bot or opponent.id == interaction.user.id:
        return await interaction.response.send_message("Choose a different scholar.", ephemeral=True)
    em = discord.Embed(
        title="⚔️ A Duel Has Been Issued",
        description=(
            f"{interaction.user.mention} challenges {opponent.mention} to a **Trivia Duel**.\n"
            f"Both ante **{DUEL_ANTE} Ink**. Winner takes **{DUEL_ANTE * 2} Ink**."
        ),
        color=COMEBACK_COLOR,
    )
    await interaction.response.send_message(
        content=opponent.mention,
        embed=em,
        view=DuelAcceptView(interaction.user, opponent),
    )


# ─── BOSS RAID (server-wide event) ────────────────────────────────────────────


class RaidStartModal(discord.ui.Modal, title="🐉 Summon a Boss Raid"):
    name = discord.ui.TextInput(label="Boss name", placeholder="The Finals Wyrm", max_length=60)
    hp = discord.ui.TextInput(label="HP (1 dmg per focus minute)", default=str(RAID_DEFAULT_HP), max_length=6)
    days = discord.ui.TextInput(label="Days to defeat", default=str(RAID_DURATION_DAYS), max_length=2)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hp = max(500, int(self.hp.value.strip()))
            days = max(1, int(self.days.value.strip()))
        except ValueError:
            return await interaction.response.send_message("Invalid numbers.", ephemeral=True)
        gid = str(interaction.guild.id)
        raid_db[gid] = {
            "active": True,
            "name": self.name.value.strip(),
            "hp": hp,
            "max_hp": hp,
            "started_at": time.time(),
            "expires_at": time.time() + days * 86400,
            "result": None,
        }
        save_json(RAID_FILE, raid_db)
        em = discord.Embed(
            title=f"🐉 BOSS RAID — {self.name.value.strip()}",
            description=(
                f"A boss with **{hp:,} HP** has appeared!\n"
                f"Every minute logged via `/focus` deals **1 damage**.\n"
                f"Defeat it within **{days} days**:\n"
                f"✦ **Win** → boutique **50% off** for 7 days\n"
                f"✦ **Lose** → daily blessings **halved** for 7 days"
            ),
            color=COMEBACK_COLOR,
        )
        em.set_footer(text="Use /raid_status to check progress.")
        await interaction.response.send_message(content="@everyone", embed=em)


@bot.tree.command(name="raid_start", description="Elysian: Summon a server-wide Boss Raid — opens a form.")
@owner_only
async def raid_start(interaction: discord.Interaction):
    if get_raid(interaction.guild.id):
        return await interaction.response.send_message(
            "🐉 A raid is already active. Check `/raid_status`.", ephemeral=True
        )
    await interaction.response.send_modal(RaidStartModal())


@bot.tree.command(name="raid_status", description="Elysian: Check the active Boss Raid.")
async def raid_status(interaction: discord.Interaction):
    raid = raid_db.get(str(interaction.guild.id))
    if not raid:
        return await interaction.response.send_message("No raid has ever been summoned here.", ephemeral=True)
    if raid.get("active"):
        bar_filled = int((1 - raid["hp"] / raid["max_hp"]) * 20)
        bar = "▰" * bar_filled + "▱" * (20 - bar_filled)
        remaining = max(0, int(raid["expires_at"] - time.time()))
        h = remaining // 3600
        em = discord.Embed(
            title=f"🐉 {raid['name']}",
            description=f"`{bar}`\n**HP:** {raid['hp']:.0f} / {raid['max_hp']:.0f}\n**Time left:** {h}h",
            color=COMEBACK_COLOR,
        )
        em.set_footer(text="Each /focus minute deals 1 damage.")
    else:
        result = raid.get("result")
        if result == "won":
            em = discord.Embed(
                title=f"🏆 {raid['name']} — DEFEATED",
                description="Boutique is **50% off** for 7 days from defeat.",
                color=0xFFD700,
            )
        elif result == "lost":
            em = discord.Embed(
                title=f"💀 {raid['name']} — STANDS",
                description="Daily blessings **halved** for 7 days.",
                color=COMEBACK_COLOR,
            )
        else:
            em = discord.Embed(title=f"🐉 {raid['name']}", description="*Idle*", color=0x95A5A6)
    await interaction.response.send_message(embed=em)


# ─── HELP ─────────────────────────────────────────────────────────────────────

MEMBER_GROUPS = [
    ("📊 Profile & Economy", [
        ("/profile", "View your scholar profile card (or Comeback Card)."),
        ("/leaderboard", "See the top scholars by study hours."),
        ("/daily", "Collect your daily Ink blessing."),
        ("/shop", "Browse the boutique and spend your Ink."),
    ]),
    ("📚 Study & Focus", [
        ("/focus", "Start a focus session and earn Ink."),
        ("/endfocus", "End your focus session and claim Ink."),
        ("/pomodoro", "Begin a Pomodoro work/break timer."),
        ("/stoppomodoro", "Cancel your active Pomodoro timer."),
        ("/deepwork", "Enter a long, distraction-free study mode."),
        ("/task", "Commit to a daily task."),
        ("/post_resource", "Share a study resource."),
    ]),
    ("🩸 High-Stakes", [
        ("/vow", "Sign the Ink Pact (Academic Comeback)."),
        ("/end_vow", "Forfeit your active Pact (Ink lost)."),
        ("/shame_board", "View active Vows and recent failures."),
        ("/gambit", "Bet Ink on a focused session — x3 if you hold."),
        ("/quit_gambit", "Forfeit your Gambit (lose stake)."),
        ("/duel", "Challenge a scholar to a Trivia Duel."),
        ("/raid_status", "Check the active Boss Raid."),
    ]),
    ("🔮 Oracle (AI)", [
        ("/ask", "Ask Elysian's oracle a question."),
        ("/summarize", "Let the oracle summarise text for you."),
        ("/oracle_challenge", "Socratic mode — Oracle refuses for 3 turns."),
        ("/simplify", "ELI5: a concept in 3 child-simple bullets."),
        ("/critique", "Harsh-professor review of your writing."),
        ("/quiz_me", "One-shot 5-question quiz from your material."),
        ("/ledger", "Your Weakness Ledger — subject heat map."),
        ("DM me", "Private 1-on-1 chat — pick a persona."),
    ]),
]

ADMIN_GROUPS = [
    ("⚙️ Server Setup", [
        ("/elysian_genesis", "Bootstrap roles & channels."),
        ("/broadcast", "Post a server-wide announcement."),
        ("/embed", "Build a beautiful custom embed."),
        ("/set_welcome", "Set the welcome message."),
        ("/set_goodbye", "Set the goodbye message."),
        ("/raid_start", "Summon a server-wide Boss Raid."),
    ]),
    ("📜 Embed Templates", [
        ("/template_save", "Save a reusable embed template."),
        ("/template_post", "Post a saved template (dropdown)."),
        ("/template_list", "List all saved templates."),
        ("/template_delete", "Delete a saved template (dropdown)."),
    ]),
    ("💎 Economy Admin", [
        ("/admin_add_item", "Add a new item to the boutique."),
        ("/set_ink", "Adjust a scholar's Ink balance."),
    ]),
    ("⚖️ Moderation", [
        ("/mute", "Silence a scholar."),
        ("/warn", "Issue a warning."),
        ("/warnings", "View a scholar's warning record."),
        ("/kick", "Remove a scholar."),
        ("/ban", "Exile a scholar permanently."),
        ("/purge", "Delete recent messages."),
        ("/purge_user", "Delete a specific user's messages."),
        ("/nuke", "Wipe and recreate this channel."),
        ("/slowmode", "Set slowmode delay."),
    ]),
    ("🔒 Channel Control", [
        ("/lock", "Freeze the current channel."),
        ("/unlock", "Unfreeze the current channel."),
        ("/lockdown_server", "Freeze every public channel."),
        ("/vault_view", "View the vault's moderation log."),
    ]),
]


def _format_group(cmds):
    return "\n".join(f"**`{n}`** — {d}" for n, d in cmds)


def _help_embed(is_admin: bool) -> discord.Embed:
    member_total = sum(len(c) for _, c in MEMBER_GROUPS)
    admin_total = sum(len(c) for _, c in ADMIN_GROUPS)
    em = discord.Embed(
        title="📖 Elysian's Grimoire — Command Codex",
        description=(
            "Every command opens a form, dropdown, or button — no typing required.\n"
            f"**🎓 Scholar Commands** *(for everyone, {member_total} total)*"
        ),
        color=0x7B5EA7,
    )
    for title, cmds in MEMBER_GROUPS:
        em.add_field(name=title, value=_format_group(cmds), inline=False)

    if is_admin:
        em.add_field(
            name="\u200b",
            value=f"**🛡️ Guardian Commands** *(owner only, {admin_total} total)*",
            inline=False,
        )
        for title, cmds in ADMIN_GROUPS:
            em.add_field(name=title, value=_format_group(cmds), inline=False)
    else:
        em.add_field(
            name="🛡️ Guardian Commands · Owner Only",
            value="*These commands are reserved for the Library's keeper.*",
            inline=False,
        )
    em.set_footer(
        text=f"Total: {member_total + admin_total} commands · ✦ Elysian, Guardian of the Library"
    )
    return em


@bot.tree.command(
    name="help", description="Elysian: Reveal the full grimoire of commands."
)
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=_help_embed(is_owner(interaction)), ephemeral=True
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if not TOKEN:
    print("ERROR: DISCORD_TOKEN is not set.")
    exit(1)

bot.run(TOKEN)
