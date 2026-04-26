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
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        print(f"Gemini init failed: {e}")

# ─── DATA FILES ───────────────────────────────────────────────────────────────

WARNS_FILE = "warns.json"
USERS_FILE = "users.json"
SHOP_FILE = "shop.json"
CONFIG_FILE = "guild_config.json"
SESSIONS_FILE = "sessions.json"
TASKS_FILE = "tasks.json"


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

# ─── RUNTIME STATE ────────────────────────────────────────────────────────────

invite_cache = {}
message_tracker = defaultdict(list)
study_warn_cd = {}
voice_join_times = {}
pomodoro_tasks = {}
deepwork_tasks = {}

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

HOUR_TIERS = [
    (100, "100h Immortal"),
    (50, "50h Sage"),
    (10, "10h Scholar"),
]
TIER_COLORS = {"10h Scholar": 0xC0C0C0, "50h Sage": 0x4169E1, "100h Immortal": 0xFFD700}
HOUR_MILESTONES = [1000, 2500, 5000, 10000]

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
        }
    return users_db[uid]


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
        print("✧ Elysian is online. Guardian of the Library is active. ✧")

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


async def _purchase_item(interaction: discord.Interaction, item_idx: int):
    """Shared purchase flow used by the shop dropdown's Buy button."""
    items = shop_db.get("items", [])
    if item_idx < 0 or item_idx >= len(items):
        return await interaction.response.send_message(
            "That item is no longer available.", ephemeral=True
        )
    item = items[item_idx]
    data = get_user(str(interaction.user.id))
    if data["ink"] < item["price"]:
        return await interaction.response.send_message(
            f"Not enough Ink. You have **{data['ink']:,}**, need **{item['price']:,}**.",
            ephemeral=True,
        )
    data["ink"] -= item["price"]
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
    data = get_user(str(target.id))
    hours = data.get("total_hours", 0)
    tier = current_tier(hours)
    s = data.get("streak", 0)
    em = discord.Embed(
        title=f"✦ {target.display_name} {streak_icon(s)}",
        color=TIER_COLORS.get(tier, 0x7B5EA7),
    )
    em.set_thumbnail(url=target.display_avatar.url)
    em.add_field(name="💧 Ink", value=f"{data.get('ink', 0):,}", inline=True)
    em.add_field(name="⏱️ Study Hours", value=f"{hours:.1f}h", inline=True)
    em.add_field(name="🔥 Streak", value=f"{s} day(s)", inline=True)
    em.add_field(name="🎓 Rank", value=tier, inline=True)
    uid = str(target.id)
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
    data = get_user(str(interaction.user.id))
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("last_daily") == today:
        return await interaction.response.send_message(
            "✨ Already collected today's blessing. Return tomorrow.", ephemeral=True
        )
    amount = random.randint(5, 20)
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
    data = get_user(uid)
    old_streak = data.get("streak", 0)
    data["ink"] += int(elapsed_h * 10)
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
    await auto_role(interaction.user, data["total_hours"])
    await check_server_milestone(interaction.guild)
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role and study_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(study_role)
        except Exception:
            pass
    em = discord.Embed(
        title="✅ Focus Session Complete",
        description=f"*{sess['topic']}*",
        color=0x57F287,
    )
    em.add_field(name="⏱️ Duration", value=f"{elapsed_h * 60:.0f} minutes", inline=True)
    em.add_field(
        name="💧 Ink Earned", value=f"{int(elapsed_h * 10)}{bonus}", inline=True
    )
    em.add_field(name="🔥 Streak", value=f"{data['streak']} day(s)", inline=True)
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


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if not TOKEN:
    print("ERROR: DISCORD_TOKEN is not set.")
    exit(1)

bot.run(TOKEN)
