import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import time
import unicodedata
import random
from datetime import timedelta, datetime, timezone
from collections import defaultdict

TOKEN     = os.environ.get("DISCORD_TOKEN")
OWNER_ID  = 1456572804815261858

# ─── GEMINI SETUP ─────────────────────────────────────────────────────────────

GEMINI_KEY   = os.environ.get("GEMINI_API_KEY")
gemini_model = None
if GEMINI_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_KEY)
        gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        print(f"Gemini init failed: {e}")

# ─── DATA FILES ───────────────────────────────────────────────────────────────

WARNS_FILE    = "warns.json"
USERS_FILE    = "users.json"
SHOP_FILE     = "shop.json"
CONFIG_FILE   = "guild_config.json"
SESSIONS_FILE = "sessions.json"

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

warns_db    = load_json(WARNS_FILE,    {})
users_db    = load_json(USERS_FILE,    {})
shop_db     = load_json(SHOP_FILE,     {"items": []})
guild_cfg   = load_json(CONFIG_FILE,   {})
sessions_db = load_json(SESSIONS_FILE, {})

# ─── RUNTIME STATE ────────────────────────────────────────────────────────────

invite_cache     = {}
message_tracker  = defaultdict(list)
study_warn_cd    = {}
voice_join_times = {}
pomodoro_tasks   = {}   # uid -> asyncio.Task
deepwork_tasks   = {}   # uid -> asyncio.Task

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

HOUR_TIERS = [
    (100, "100h Immortal"),
    (50,  "50h Sage"),
    (10,  "10h Scholar"),
]
TIER_NAMES    = {name for _, name in HOUR_TIERS}
TIER_COLORS   = {"10h Scholar": 0xC0C0C0, "50h Sage": 0x4169E1, "100h Immortal": 0xFFD700}
HOUR_MILESTONES = [1000, 2500, 5000, 10000]

# ─── UTILITY FUNCTIONS ────────────────────────────────────────────────────────

def get_user(uid: str) -> dict:
    uid = str(uid)
    if uid not in users_db:
        users_db[uid] = {
            "ink": 0, "total_hours": 0.0,
            "streak": 0, "last_study_date": None, "last_daily": None,
        }
    return users_db[uid]

def gcfg(guild_id: int) -> dict:
    return guild_cfg.setdefault(str(guild_id), {})

def vault_cid(guild_id: int) -> int:
    return gcfg(guild_id).get("vault_channel_id", 1484495219176243200)

async def get_vault(guild: discord.Guild):
    return guild.get_channel(vault_cid(guild.id))

def clean_nickname(name: str) -> str:
    cleaned = "".join(
        c for c in name
        if not unicodedata.category(c).startswith("C")
    ).strip()
    return cleaned or "Scholar"

def fill_vars(text: str, member: discord.Member) -> str:
    """Replace template variables with real values."""
    return (text
        .replace("{user.name}",      member.display_name)
        .replace("{user.mention}",   member.mention)
        .replace("{user.id}",        str(member.id))
        .replace("{server.name}",    member.guild.name)
        .replace("{server.members}", str(member.guild.member_count))
    )

def server_total_hours(guild_id: int) -> float:
    return sum(d.get("total_hours", 0) for d in users_db.values())

def current_tier(hours: float) -> str:
    for req, name in HOUR_TIERS:
        if hours >= req:
            return name
    return "Unranked"

def streak_icon(streak: int) -> str:
    if streak >= 7:  return "🔥🔥"
    if streak >= 3:  return "🔥"
    return ""

# ─── ROLE AUTO-PROMOTION ──────────────────────────────────────────────────────

async def auto_role(member: discord.Member, hours: float):
    earned = current_tier(hours)
    if earned == "Unranked":
        return
    if discord.utils.get(member.roles, name=earned):
        return  # Already has it

    guild = member.guild
    for _, name in HOUR_TIERS:
        old = discord.utils.get(guild.roles, name=name)
        if old and old in member.roles:
            try:
                await member.remove_roles(old, reason="Elysian tier progression")
            except Exception:
                pass

    new_role = discord.utils.get(guild.roles, name=earned)
    if not new_role:
        return
    try:
        await member.add_roles(new_role, reason="Elysian tier progression")
    except Exception:
        return

    cfg = gcfg(guild.id)
    cid = cfg.get("leaderboard_channel_id") or cfg.get("vault_channel_id")
    ch  = guild.get_channel(cid) if cid else None
    if ch:
        embed = discord.Embed(
            title="✨ Tier Ascension",
            description=f"**{member.mention}** has ascended to **{new_role.mention}**!\n*The library acknowledges your dedication.*",
            color=TIER_COLORS.get(earned, 0x7B5EA7)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Elysian Prestige System")
        await ch.send(embed=embed)

async def check_server_milestone(guild: discord.Guild):
    """Announce when the server crosses a study-hour milestone."""
    cfg = gcfg(guild.id)
    total = server_total_hours(guild.id)
    last  = cfg.get("last_milestone", 0)

    for ms in HOUR_MILESTONES:
        if total >= ms > last:
            cfg["last_milestone"] = ms
            save_json(CONFIG_FILE, guild_cfg)
            cid = cfg.get("leaderboard_channel_id") or cfg.get("vault_channel_id")
            ch  = guild.get_channel(cid) if cid else None
            if ch:
                embed = discord.Embed(
                    title=f"🎉 MILESTONE — {ms:,} HOURS STUDIED!",
                    description=(
                        f"**The Elysian Library has collectively studied {ms:,} hours!**\n\n"
                        f"✦ Every candle lit, every page turned — it all counts.\n"
                        f"*The Architect is proud. The library grows eternal.*"
                    ),
                    color=0xFFD700
                )
                embed.set_footer(text="Elysian Prestige System • Digital Party Time 🎊")
                await ch.send("@everyone", embed=embed)
            break

async def mourn_streak(member: discord.Member, old_streak: int):
    """DM a user when they break their study streak."""
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

# ─── BOT CLASS ────────────────────────────────────────────────────────────────

class Elysian(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="e!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        self.passive_ink_task.start()
        print("✧ Elysian is online. Guardian of the Library is active. ✧")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
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
            d["ink"]        += 10
            d["total_hours"] = round(d.get("total_hours", 0) + 1, 2)
        save_json(USERS_FILE, users_db)

    @passive_ink_task.before_loop
    async def before_passive_ink(self):
        await self.wait_until_ready()


bot = Elysian()

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID

# ─── EVENT: MESSAGES ──────────────────────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    now = time.time()
    uid = str(message.author.id)

    # Anti-Spam Shield: 5+ images at once
    images = sum(
        1 for a in message.attachments
        if any(a.filename.lower().endswith(x) for x in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
    )
    if images >= 5:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} The Gallery is full. Please wait a moment.", delete_after=8
            )
        except Exception:
            pass
        return

    # Study Warden: nudge if 10+ messages in 60 seconds
    message_tracker[uid].append(now)
    message_tracker[uid] = [t for t in message_tracker[uid] if now - t < 60]
    if len(message_tracker[uid]) >= 10:
        last_warned = study_warn_cd.get(uid, 0)
        if now - last_warned > 600:
            study_warn_cd[uid] = now
            message_tracker[uid] = []
            try:
                await message.author.send(
                    "📚 *Scholar, your books are waiting.* You've been very active in chat. "
                    "Shall I mute this channel for you so you can focus?"
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

    # Ghost Ping Protection
    if message.mentions:
        pinged = ", ".join(m.mention for m in message.mentions)
        embed = discord.Embed(title="👻 Ghost Ping Detected", color=0xff6b6b)
        embed.add_field(name="Pinger",          value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Pinged",           value=pinged, inline=False)
        embed.add_field(name="Channel",          value=message.channel.mention, inline=False)
        embed.add_field(name="Deleted Message",  value=message.content or "*(no text)*", inline=False)
        embed.set_footer(text="Elysian Vault • Ghost Ping")
        await vault.send(embed=embed)
    else:
        embed = discord.Embed(title="🗑️ Message Deleted", color=0xff4d4d)
        embed.add_field(name="Scholar",  value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Location", value=message.channel.mention, inline=False)
        embed.add_field(name="Content",  value=message.content or "*(attachment only)*", inline=False)
        embed.set_footer(text="Elysian Vault • Deleted Message")
        await vault.send(embed=embed)

    # Vanished Media Recovery
    for att in message.attachments:
        if any(att.filename.lower().endswith(x) for x in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            em = discord.Embed(
                title="📷 Vanished Media Recovered",
                description=f"Deleted by {message.author.mention} in {message.channel.mention}",
                color=0xffa500
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
    embed = discord.Embed(title="✏️ Shadow Edit Detected", color=0xffcc00)
    embed.add_field(name="Scholar",  value=before.author.mention, inline=False)
    embed.add_field(name="Original", value=before.content or "*(empty)*", inline=False)
    embed.add_field(name="Revised",  value=after.content  or "*(empty)*", inline=False)
    embed.add_field(name="Channel",  value=before.channel.mention, inline=False)
    embed.set_footer(text="Elysian Vault • Shadow Edit")
    await vault.send(embed=embed)


@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        vault = await get_vault(after.guild)
        if vault:
            added   = [r for r in after.roles  if r not in before.roles]
            removed = [r for r in before.roles  if r not in after.roles]
            if added:
                em = discord.Embed(title="🎭 Role Stealth — Role Added", color=0x57f287)
                em.add_field(name="Member",     value=after.mention, inline=False)
                em.add_field(name="Role Added", value=", ".join(r.mention for r in added), inline=False)
                em.set_footer(text="Elysian Vault • Role Stealth")
                await vault.send(embed=em)
            if removed:
                em = discord.Embed(title="🎭 Role Stealth — Role Removed", color=0xed4245)
                em.add_field(name="Member",       value=after.mention, inline=False)
                em.add_field(name="Role Removed", value=", ".join(r.mention for r in removed), inline=False)
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
    # Auto-Nickname
    cleaned = clean_nickname(member.display_name)
    if cleaned != member.display_name:
        try:
            await member.edit(nick=cleaned, reason="Elysian Auto-Nickname")
        except Exception:
            pass

    guild = member.guild
    cfg   = gcfg(guild.id)

    # Welcome embed
    tmpl = cfg.get("welcome_template")
    if tmpl:
        cid = cfg.get("welcome_channel_id") or cfg.get("leaderboard_channel_id")
        ch  = guild.get_channel(cid) if cid else None
        if ch:
            try:
                color = int(tmpl.get("color", "7B5EA7"), 16)
            except Exception:
                color = 0x7B5EA7
            em = discord.Embed(
                title=fill_vars(tmpl.get("title", "Welcome!"), member),
                description=fill_vars(tmpl.get("description", ""), member),
                color=color
            )
            em.set_thumbnail(url=member.display_avatar.url)
            if tmpl.get("image"):
                em.set_image(url=tmpl["image"])
            if tmpl.get("footer"):
                em.set_footer(text=fill_vars(tmpl["footer"], member))
            await ch.send(embed=em)

    # Invite Watch → vault
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

        em = discord.Embed(title="🔗 Invite Watch — New Member", color=0x5865f2)
        em.set_thumbnail(url=member.display_avatar.url)
        em.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        if used:
            em.add_field(name="Invite Code", value=f"`{used.code}`",                             inline=True)
            em.add_field(name="Created By",  value=used.inviter.mention if used.inviter else "?", inline=True)
            em.add_field(name="Total Uses",  value=str(used.uses),                               inline=True)
        else:
            em.add_field(name="Invite", value="Could not determine.", inline=False)
        em.set_footer(text="Elysian Vault • Invite Watch")
        await vault.send(embed=em)
    except Exception:
        pass


@bot.event
async def on_member_remove(member):
    guild = member.guild
    cfg   = gcfg(guild.id)
    tmpl  = cfg.get("goodbye_template")
    if not tmpl:
        return
    cid = cfg.get("welcome_channel_id") or cfg.get("leaderboard_channel_id")
    ch  = guild.get_channel(cid) if cid else None
    if not ch:
        return
    try:
        color = int(tmpl.get("color", "ed4245"), 16)
    except Exception:
        color = 0xed4245
    em = discord.Embed(
        title=fill_vars(tmpl.get("title", "Farewell!"), member),
        description=fill_vars(tmpl.get("description", ""), member),
        color=color
    )
    em.set_thumbnail(url=member.display_avatar.url)
    if tmpl.get("footer"):
        em.set_footer(text=fill_vars(tmpl["footer"], member))
    await ch.send(embed=em)


@bot.event
async def on_voice_state_update(member, before, after):
    uid   = str(member.id)
    cfg   = gcfg(member.guild.id)
    svcs  = cfg.get("study_vc_ids", [])

    joined_study = after.channel and after.channel.id in svcs and (not before.channel or before.channel.id not in svcs)
    left_study   = before.channel and before.channel.id in svcs and (not after.channel or after.channel.id not in svcs)

    if joined_study:
        voice_join_times[uid] = time.time()

    if left_study:
        join_ts = voice_join_times.pop(uid, None)
        if not join_ts:
            return

        elapsed_h = (time.time() - join_ts) / 3600
        ink_earn  = int(elapsed_h * 10)
        data      = get_user(uid)
        old_streak = data.get("streak", 0)

        data["ink"]         += ink_earn
        data["total_hours"]  = round(data.get("total_hours", 0) + elapsed_h, 2)

        today     = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        last      = data.get("last_study_date")

        if last and last != today and last != yesterday:
            # Streak broken
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

# ─── GENESIS: AUTO-SETUP ──────────────────────────────────────────────────────

@bot.tree.command(name="setup_elysian", description="Elysian: One-time server setup — creates all roles, channels, and categories.")
async def setup_elysian(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    cfg   = gcfg(guild.id)

    # Category
    cat = discord.utils.get(guild.categories, name="✧ ELYSIAN PRESTIGE ✧") or \
          await guild.create_category("✧ ELYSIAN PRESTIGE ✧")

    # Roles
    role_specs = [
        ("10h Scholar",              discord.Color.from_str("#C0C0C0")),
        ("50h Sage",                 discord.Color.from_str("#4169E1")),
        ("100h Immortal",            discord.Color.from_str("#FFD700")),
        ("Library VIP",              discord.Color.from_str("#9B59B6")),
        ("The Architect's Favorite", discord.Color.from_str("#FF8C00")),
        ("Elite Monthly Rank",       discord.Color.from_str("#FF69B4")),
        ("📚 Study",                  discord.Color.from_str("#7B5EA7")),
    ]
    for name, color in role_specs:
        if not discord.utils.get(guild.roles, name=name):
            await guild.create_role(name=name, color=color, reason="Elysian Genesis")

    # Vault channel (owner-only)
    vault_ch = discord.utils.get(guild.text_channels, name="vault-logs")
    if not vault_ch:
        ow = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        owner = guild.get_member(OWNER_ID)
        if owner:
            ow[owner] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
        vault_ch = await guild.create_text_channel("vault-logs", category=cat, overwrites=ow)
    cfg["vault_channel_id"] = vault_ch.id

    # Shop channel
    shop_ch = discord.utils.get(guild.text_channels, name="elysian-shop") or \
              await guild.create_text_channel("elysian-shop", category=cat)
    cfg["shop_channel_id"] = shop_ch.id

    # Leaderboard channel
    lb_ch = discord.utils.get(guild.text_channels, name="leaderboard-hall") or \
            await guild.create_text_channel("leaderboard-hall", category=cat)
    cfg["leaderboard_channel_id"] = lb_ch.id

    # Welcome channel (general)
    welcome_ch = discord.utils.get(guild.text_channels, name="welcome") or \
                 await guild.create_text_channel("welcome", category=cat)
    cfg["welcome_channel_id"] = welcome_ch.id

    # Study VC
    study_vc = discord.utils.get(guild.voice_channels, name="📚 Study Hall") or \
               await guild.create_voice_channel("📚 Study Hall", category=cat)
    cfg.setdefault("study_vc_ids", [])
    if study_vc.id not in cfg["study_vc_ids"]:
        cfg["study_vc_ids"].append(study_vc.id)

    # General channel reference for Deep Work
    for ch in guild.text_channels:
        if ch.name in ("general", "general-chat", "chat"):
            cfg["general_channel_id"] = ch.id
            break

    save_json(CONFIG_FILE, guild_cfg)

    em = discord.Embed(title="✧ Elysian Genesis Complete ✧", description="The library has been established.", color=0x7B5EA7)
    em.add_field(name="📂 Category", value="✧ ELYSIAN PRESTIGE ✧", inline=False)
    em.add_field(name="🎭 Roles",    value="\n".join(f"• {n}" for n, _ in role_specs), inline=True)
    em.add_field(name="📋 Channels", value=f"• {vault_ch.mention}\n• {shop_ch.mention}\n• {lb_ch.mention}\n• {welcome_ch.mention}", inline=True)
    em.add_field(name="🎙️ Study VC", value=study_vc.mention, inline=False)
    em.set_footer(text="Elysian Prestige System")
    await interaction.followup.send(embed=em, ephemeral=True)

# ─── CLEANSE ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="purge", description="Elysian: Delete a number of recent messages.")
@app_commands.describe(amount="Number of messages to delete")
async def purge(interaction: discord.Interaction, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Cleansed **{len(deleted)}** messages.", ephemeral=True)

@bot.tree.command(name="purge_user", description="Elysian: Delete messages from a specific user.")
@app_commands.describe(user="Target user", amount="Messages to scan")
async def purge_user(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.author.id == user.id)
    await interaction.followup.send(f"Removed **{len(deleted)}** messages from **{user.display_name}**.", ephemeral=True)

@bot.tree.command(name="nuke", description="Elysian: Wipe and recreate this channel.")
async def nuke(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ch = interaction.channel
    new_ch = await ch.clone(reason="Elysian Nuke")
    await new_ch.edit(position=ch.position)
    await ch.delete(reason="Elysian Nuke")
    await new_ch.send("🌌 *The library has been purified. A new chapter begins.*", delete_after=12)

@bot.tree.command(name="slowmode", description="Elysian: Set slowmode delay.")
@app_commands.describe(seconds="Delay in seconds (0 to disable)")
async def slowmode(interaction: discord.Interaction, seconds: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds)
    msg = "⏱️ Slowmode lifted. The flow of time is restored." if seconds == 0 else f"⏱️ Slowmode set to **{seconds}s**."
    await interaction.response.send_message(embed=discord.Embed(description=msg, color=0xffa500))

# ─── SILENCE ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="mute", description="Elysian: Silence a scholar with a timeout.")
@app_commands.describe(user="The user to mute", minutes="Duration in minutes", reason="Reason")
async def mute(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    try:
        await user.timeout(timedelta(minutes=minutes), reason=reason)
        try:
            await user.send(
                f"🌿 You have been moved to the Silent Gardens for: **{reason}**\n"
                f"Duration: **{minutes} minute(s)**. Reflect, and return with clarity."
            )
        except Exception:
            pass
        await interaction.response.send_message(embed=discord.Embed(
            description=f"🌿 {user.mention} entered the Silent Gardens for **{minutes}m**.\nReason: *{reason}*",
            color=0x7b5ea7
        ))
    except discord.Forbidden:
        await interaction.response.send_message("I lack the authority to silence this scholar.", ephemeral=True)

@bot.tree.command(name="warn", description="Elysian: Add a strike to a scholar's record.")
@app_commands.describe(user="The user to warn", reason="Reason")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    uid = str(user.id)
    warns_db.setdefault(uid, {"username": str(user), "warnings": []})
    warns_db[uid]["warnings"].append({"reason": reason, "timestamp": discord.utils.utcnow().isoformat()})
    save_json(WARNS_FILE, warns_db)
    count = len(warns_db[uid]["warnings"])
    await interaction.response.send_message(embed=discord.Embed(
        description=f"📜 **{user.display_name}** received strike #{count}.\nReason: *{reason}*", color=0xffa500
    ))
    try:
        await user.send(f"⚠️ Warning in **{interaction.guild.name}**: **{reason}**")
    except Exception:
        pass

@bot.tree.command(name="warnings", description="Elysian: View a scholar's warning record.")
@app_commands.describe(user="The scholar to inspect")
async def warnings(interaction: discord.Interaction, user: discord.Member):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    uid = str(user.id)
    if uid not in warns_db or not warns_db[uid]["warnings"]:
        return await interaction.response.send_message(
            f"📖 **{user.display_name}**'s record is pristine.", ephemeral=True
        )
    data = warns_db[uid]["warnings"]
    em = discord.Embed(title=f"📜 Scholar Record — {user.display_name}", description=f"**{len(data)}** warning(s)", color=0xffa500)
    em.set_thumbnail(url=user.display_avatar.url)
    for i, w in enumerate(data, 1):
        em.add_field(name=f"Strike #{i} — {w.get('timestamp','')[:10]}", value=w["reason"], inline=False)
    em.set_footer(text="Elysian Vault • Scholar Record")
    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="kick", description="Elysian: Remove a scholar from the library.")
@app_commands.describe(user="The user to kick", reason="Reason")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    try:
        await user.send(f"👣 Removed from **{interaction.guild.name}**. Reason: **{reason}**")
    except Exception:
        pass
    await user.kick(reason=reason)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"👣 **{user.display_name}** escorted out. Reason: *{reason}*", color=0xed4245
    ))

@bot.tree.command(name="ban", description="Elysian: Permanently exile a scholar.")
@app_commands.describe(user="The user to ban", reason="Reason")
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    try:
        await user.send(f"🔒 Exiled from **{interaction.guild.name}**. Reason: **{reason}**")
    except Exception:
        pass
    await user.ban(reason=reason, delete_message_days=0)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"🔒 **{user.display_name}** exiled. Reason: *{reason}*", color=0xed4245
    ))

# ─── FORTRESS ─────────────────────────────────────────────────────────────────

@bot.tree.command(name="lock", description="Elysian: Freeze the current channel.")
async def lock(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(embed=discord.Embed(
        description="🔒 *The gates are sealed. This chamber demands silence.*", color=0x2f3136
    ))

@bot.tree.command(name="lockdown_server", description="Elysian: Freeze every public channel at once.")
async def lockdown_server(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
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
    await interaction.followup.send(f"🔒 Fortress activated. **{locked}** channels sealed.", ephemeral=True)

@bot.tree.command(name="unlock", description="Elysian: Restore the flow to the current channel.")
async def unlock(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = True
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(embed=discord.Embed(
        description="🔓 *The gates are open. The library welcomes all scholars.*", color=0x57f287
    ))

# ─── ARCHITECT COMMANDS ───────────────────────────────────────────────────────

@bot.tree.command(name="set_ink", description="Elysian: Manually set a user's Ink balance.")
@app_commands.describe(user="The scholar", amount="New Ink amount")
async def set_ink(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    get_user(str(user.id))["ink"] = amount
    save_json(USERS_FILE, users_db)
    await interaction.response.send_message(f"💧 **{user.display_name}**'s Ink set to **{amount}**.", ephemeral=True)

@bot.tree.command(name="broadcast", description="Elysian: Send a styled announcement to all channels.")
@app_commands.describe(message="The message to broadcast")
async def broadcast(interaction: discord.Interaction, message: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    em = discord.Embed(title="📣 Elysian Announcement", description=message, color=0x7B5EA7)
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
    await interaction.followup.send(f"📣 Broadcast sent to **{sent}** channels.", ephemeral=True)

@bot.tree.command(name="vault_view", description="Elysian: View a summary of recent vault events.")
async def vault_view(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    em = discord.Embed(title="👁️ Vault Summary", color=0x7B5EA7)
    total_warns = sum(len(v.get("warnings", [])) for v in warns_db.values())
    em.add_field(name="⚠️ Total Warnings",  value=str(total_warns),    inline=True)
    em.add_field(name="👥 Scholars on File", value=str(len(warns_db)), inline=True)
    recent = sorted(
        [(d.get("username", uid), w["reason"], w.get("timestamp","")[:10])
         for uid, d in warns_db.items() for w in d.get("warnings", [])],
        key=lambda x: x[2], reverse=True
    )[:5]
    em.add_field(
        name="📋 Recent Warnings",
        value="\n".join(f"• **{u}** — {r} `{ts}`" for u, r, ts in recent) or "None.",
        inline=False
    )
    em.set_footer(text="Elysian Vault • Summary")
    await interaction.response.send_message(embed=em, ephemeral=True)

# ─── SCHOLAR COMMANDS ─────────────────────────────────────────────────────────

@bot.tree.command(name="profile", description="Elysian: View your scholar profile card.")
@app_commands.describe(user="Scholar to view (leave blank for yourself)")
async def profile(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    data   = get_user(str(target.id))
    hours  = data.get("total_hours", 0)
    tier   = current_tier(hours)
    s      = data.get("streak", 0)
    color  = TIER_COLORS.get(tier, 0x7B5EA7)

    em = discord.Embed(title=f"✦ {target.display_name} {streak_icon(s)}", color=color)
    em.set_thumbnail(url=target.display_avatar.url)
    em.add_field(name="💧 Ink",         value=f"{data.get('ink',0):,}",   inline=True)
    em.add_field(name="⏱️ Study Hours", value=f"{hours:.1f}h",            inline=True)
    em.add_field(name="🔥 Streak",      value=f"{s} day(s)",              inline=True)
    em.add_field(name="🎓 Rank",        value=tier,                        inline=True)

    uid = str(target.id)
    if uid in sessions_db:
        elapsed = (time.time() - sessions_db[uid]["start"]) / 60
        em.add_field(name="📚 Active Focus", value=f"*{sessions_db[uid]['topic']}* — {elapsed:.0f}m", inline=False)
    if uid in voice_join_times:
        vc_elapsed = (time.time() - voice_join_times[uid]) / 60
        em.add_field(name="🎙️ In Study VC", value=f"{vc_elapsed:.0f}m active", inline=False)

    em.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=em)

@bot.tree.command(name="leaderboard", description="Elysian: View the top scholars by study hours.")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    if not users_db:
        return await interaction.followup.send("No scholars on record yet.")

    top = sorted(users_db.items(), key=lambda x: x[1].get("total_hours", 0), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + ["✦"] * 7

    em = discord.Embed(title="🏛️ Elysian Leaderboard — Hall of Scholars", color=0xFFD700)
    for i, (uid, d) in enumerate(top):
        m     = interaction.guild.get_member(int(uid))
        name  = m.display_name if m else f"Scholar"
        hours = d.get("total_hours", 0)
        s     = d.get("streak", 0)
        icon  = streak_icon(s)
        tier  = current_tier(hours)
        em.add_field(
            name=f"{medals[i]} #{i+1} — {name} {icon}",
            value=f"⏱️ `{hours:.1f}h` • 💧 `{d.get('ink',0):,} Ink` • 🎓 {tier}" + (f" • 🔥 {s}d" if s >= 3 else ""),
            inline=False
        )
    em.set_footer(text=f"Total server hours: {server_total_hours(interaction.guild.id):.1f}h • Elysian Prestige")
    await interaction.followup.send(embed=em)

@bot.tree.command(name="daily", description="Elysian: Collect your daily Ink blessing.")
async def daily(interaction: discord.Interaction):
    data  = get_user(str(interaction.user.id))
    today = datetime.now(timezone.utc).date().isoformat()
    if data.get("last_daily") == today:
        return await interaction.response.send_message(
            "✨ You've already collected today's blessing. Return tomorrow, scholar.", ephemeral=True
        )
    amount = random.randint(5, 20)
    data["ink"]       += amount
    data["last_daily"]  = today
    save_json(USERS_FILE, users_db)
    em = discord.Embed(title="✨ Daily Blessing", description=f"The library bestows **{amount} Ink** upon you.", color=0xFFD700)
    em.add_field(name="💧 Total Ink", value=f"{data['ink']:,}")
    em.set_footer(text="Return tomorrow for another blessing.")
    await interaction.response.send_message(embed=em)

# ─── FOCUS / POMODORO ─────────────────────────────────────────────────────────

@bot.tree.command(name="focus", description="Elysian: Start a text-based focus session.")
@app_commands.describe(topic="What are you studying?")
async def focus(interaction: discord.Interaction, topic: str = "Open study"):
    uid = str(interaction.user.id)
    if uid in sessions_db:
        elapsed = (time.time() - sessions_db[uid]["start"]) / 60
        return await interaction.response.send_message(
            f"📚 Already in session: *{sessions_db[uid]['topic']}* — {elapsed:.0f}m in. Use `/endfocus` first.",
            ephemeral=True
        )
    sessions_db[uid] = {"start": time.time(), "topic": topic}
    save_json(SESSIONS_FILE, sessions_db)

    # Give Study role
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role:
        try:
            await interaction.user.add_roles(study_role, reason="Elysian focus session")
        except Exception:
            pass

    em = discord.Embed(
        title="📚 Focus Session Started",
        description=f"**{interaction.user.display_name}** has entered deep focus.\n*Topic: {topic}*",
        color=0x7B5EA7
    )
    em.set_footer(text="Use /endfocus to end your session and claim your Ink.")
    await interaction.response.send_message(embed=em)

@bot.tree.command(name="endfocus", description="Elysian: End your focus session and claim Ink.")
async def endfocus(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in sessions_db:
        return await interaction.response.send_message(
            "You don't have an active focus session. Use `/focus` to start one.", ephemeral=True
        )
    sess = sessions_db.pop(uid)
    save_json(SESSIONS_FILE, sessions_db)

    elapsed_h  = (time.time() - sess["start"]) / 3600
    ink_earned = int(elapsed_h * 10)
    data       = get_user(uid)
    old_streak = data.get("streak", 0)
    data["ink"]        += ink_earned
    data["total_hours"] = round(data.get("total_hours", 0) + elapsed_h, 2)

    today     = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    last      = data.get("last_study_date")

    if last and last != today and last != yesterday:
        await mourn_streak(interaction.user, old_streak)
        data["streak"] = 1
    elif last == yesterday:
        data["streak"] = old_streak + 1
    elif last != today:
        data["streak"] = 1
    data["last_study_date"] = today

    bonus_text = ""
    if data["streak"] % 3 == 0:
        data["ink"] += 5
        bonus_text = " (+5 streak bonus!)"

    save_json(USERS_FILE, users_db)
    await auto_role(interaction.user, data["total_hours"])
    await check_server_milestone(interaction.guild)

    # Remove Study role
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role and study_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(study_role, reason="Focus session ended")
        except Exception:
            pass

    em = discord.Embed(title="✅ Focus Session Complete", description=f"*{sess['topic']}*", color=0x57f287)
    em.add_field(name="⏱️ Duration",   value=f"{elapsed_h*60:.0f} minutes", inline=True)
    em.add_field(name="💧 Ink Earned", value=f"{ink_earned}{bonus_text}",    inline=True)
    em.add_field(name="🔥 Streak",     value=f"{data['streak']} day(s)",     inline=True)
    em.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=em)


async def _run_pomodoro(member: discord.Member, channel: discord.TextChannel, work_m: int, break_m: int):
    try:
        study_role = discord.utils.get(member.guild.roles, name="📚 Study")
        # Work phase
        await asyncio.sleep(work_m * 60)
        if study_role:
            try:
                await member.remove_roles(study_role, reason="Pomodoro break")
            except Exception:
                pass
        try:
            await member.send(f"⏰ **Break time!** You studied for **{work_m} minutes**. Rest for **{break_m} minutes**. 🌿")
        except Exception:
            pass
        try:
            await channel.send(f"⏰ {member.mention} — Break time! Back in {break_m}m.", delete_after=break_m * 60)
        except Exception:
            pass

        # Break phase
        await asyncio.sleep(break_m * 60)
        if study_role:
            try:
                await member.add_roles(study_role, reason="Pomodoro work resumed")
            except Exception:
                pass
        try:
            await member.send(f"📚 **Break over!** Back to work, scholar. Run `/pomodoro` again for another round.")
        except Exception:
            pass

        # Credit Ink for the work session
        data = get_user(str(member.id))
        data["ink"]        += int((work_m / 60) * 10)
        data["total_hours"] = round(data.get("total_hours", 0) + work_m / 60, 2)
        save_json(USERS_FILE, users_db)
        await auto_role(member, data["total_hours"])
    finally:
        pomodoro_tasks.pop(str(member.id), None)


@bot.tree.command(name="pomodoro", description="Elysian: Start a Pomodoro timer. Gives Study role during work, removes on break.")
@app_commands.describe(work_minutes="Work duration (default 25)", break_minutes="Break duration (default 5)")
async def pomodoro(interaction: discord.Interaction, work_minutes: int = 25, break_minutes: int = 5):
    uid = str(interaction.user.id)
    if uid in pomodoro_tasks:
        return await interaction.response.send_message(
            "You already have an active Pomodoro. Use `/stoppomodoro` to cancel it.", ephemeral=True
        )

    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role:
        try:
            await interaction.user.add_roles(study_role, reason="Pomodoro started")
        except Exception:
            pass

    task = asyncio.create_task(
        _run_pomodoro(interaction.user, interaction.channel, work_minutes, break_minutes)
    )
    pomodoro_tasks[uid] = task

    em = discord.Embed(
        title="🍅 Pomodoro Started",
        description=(
            f"**Work:** {work_minutes} minutes\n"
            f"**Break:** {break_minutes} minutes\n\n"
            f"*The Study role has been applied. Other channels will be restricted if configured.\n"
            f"The bot will DM you when it's break time.*"
        ),
        color=0x7B5EA7
    )
    em.set_footer(text="Use /stoppomodoro to cancel.")
    await interaction.response.send_message(embed=em)


@bot.tree.command(name="stoppomodoro", description="Elysian: Stop your active Pomodoro timer.")
async def stoppomodoro(interaction: discord.Interaction):
    uid  = str(interaction.user.id)
    task = pomodoro_tasks.pop(uid, None)
    if not task:
        return await interaction.response.send_message("No active Pomodoro to stop.", ephemeral=True)
    task.cancel()
    study_role = discord.utils.get(interaction.guild.roles, name="📚 Study")
    if study_role and study_role in interaction.user.roles:
        try:
            await interaction.user.remove_roles(study_role, reason="Pomodoro stopped")
        except Exception:
            pass
    await interaction.response.send_message("🍅 Pomodoro stopped. The study session has ended.", ephemeral=True)


# ─── DEEP WORK MODE ───────────────────────────────────────────────────────────

async def _restore_deepwork(member: discord.Member, channel: discord.TextChannel):
    await asyncio.sleep(0)  # starts after interaction responds
    try:
        ow = channel.overwrites_for(member)
        ow.read_messages = None  # reset to default
        await channel.set_permissions(member, overwrite=ow)
        try:
            await member.send(f"🔓 **Deep Work complete!** You can now see {channel.mention} again. Great work, scholar.")
        except Exception:
            pass
    finally:
        deepwork_tasks.pop(str(member.id), None)


@bot.tree.command(name="deepwork", description="Elysian: Hide General Chat from yourself for focused study.")
@app_commands.describe(minutes="How long to hide General Chat (default 60)")
async def deepwork(interaction: discord.Interaction, minutes: int = 60):
    uid = str(interaction.user.id)
    cfg = gcfg(interaction.guild.id)
    cid = cfg.get("general_channel_id")

    if not cid:
        return await interaction.response.send_message(
            "No General Chat channel is configured. Run `/setup_elysian` first, or make sure a channel named `general` exists.",
            ephemeral=True
        )

    ch = interaction.guild.get_channel(cid)
    if not ch:
        return await interaction.response.send_message("General Chat channel not found.", ephemeral=True)

    if uid in deepwork_tasks:
        return await interaction.response.send_message(
            "You're already in Deep Work mode. It will lift automatically.", ephemeral=True
        )

    # Hide the channel for this user
    ow = ch.overwrites_for(interaction.user)
    ow.read_messages = False
    await ch.set_permissions(interaction.user, overwrite=ow)

    # Schedule restore
    async def wait_and_restore():
        await asyncio.sleep(minutes * 60)
        await _restore_deepwork(interaction.user, ch)

    task = asyncio.create_task(wait_and_restore())
    deepwork_tasks[uid] = task

    em = discord.Embed(
        title="🧠 Deep Work Mode Activated",
        description=(
            f"{ch.mention} is now hidden from you for **{minutes} minutes**.\n\n"
            f"*The distraction has been sealed. Return to your books, scholar.*"
        ),
        color=0x2f3136
    )
    em.set_footer(text="The channel will restore automatically when the timer ends.")
    await interaction.response.send_message(embed=em, ephemeral=True)

# ─── WELCOME / GOODBYE SETUP ──────────────────────────────────────────────────

class WelcomeModal(discord.ui.Modal):
    title_field  = discord.ui.TextInput(label="Embed Title",       placeholder="Welcome to {server.name}!", max_length=256)
    desc_field   = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                        placeholder="Hey {user.mention}, welcome! We now have {server.members} members.", max_length=2048)
    color_field  = discord.ui.TextInput(label="Color (hex)",       placeholder="7B5EA7", max_length=6, required=False)
    footer_field = discord.ui.TextInput(label="Footer Text",       placeholder="Optional footer text...", required=False, max_length=200)
    image_field  = discord.ui.TextInput(label="Image URL",         placeholder="https://...", required=False)

    def __init__(self, mode: str):
        self._mode = mode
        super().__init__(title=f"Set {mode.title()} Message")

    async def on_submit(self, interaction: discord.Interaction):
        key = f"{self._mode}_template"
        gcfg(interaction.guild.id)[key] = {
            "title":       self.title_field.value,
            "description": self.desc_field.value,
            "color":       self.color_field.value.strip().lstrip("#") or ("7B5EA7" if self._mode == "welcome" else "ed4245"),
            "footer":      self.footer_field.value,
            "image":       self.image_field.value.strip(),
        }
        save_json(CONFIG_FILE, guild_cfg)
        await interaction.response.send_message(
            f"✅ {self._mode.title()} message saved! Variables supported: `{{user.name}}`, `{{user.mention}}`, `{{server.name}}`, `{{server.members}}`",
            ephemeral=True
        )


@bot.tree.command(name="set_welcome", description="Elysian: Set the welcome message for new members.")
async def set_welcome(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(WelcomeModal("welcome"))

@bot.tree.command(name="set_goodbye", description="Elysian: Set the goodbye message for departing members.")
async def set_goodbye(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(WelcomeModal("goodbye"))

# ─── TEMPLATE LIBRARY ─────────────────────────────────────────────────────────

class TemplateModal(discord.ui.Modal):
    tmpl_title   = discord.ui.TextInput(label="Title",          placeholder="Enter title...", max_length=256)
    tmpl_desc    = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                        placeholder="Supports {user.name}, {server.name} etc.", max_length=2048)
    tmpl_color   = discord.ui.TextInput(label="Color (hex)",    placeholder="7B5EA7", max_length=6, required=False)
    tmpl_footer  = discord.ui.TextInput(label="Footer",         placeholder="Optional footer...", required=False, max_length=200)
    tmpl_image   = discord.ui.TextInput(label="Image URL",      placeholder="https://...", required=False)

    def __init__(self, name: str):
        self._name = name
        super().__init__(title=f"Save Template: {name[:40]}")

    async def on_submit(self, interaction: discord.Interaction):
        cfg = gcfg(interaction.guild.id)
        cfg.setdefault("templates", {})[self._name] = {
            "title":       self.tmpl_title.value,
            "description": self.tmpl_desc.value,
            "color":       self.tmpl_color.value.strip().lstrip("#") or "7B5EA7",
            "footer":      self.tmpl_footer.value,
            "image":       self.tmpl_image.value.strip(),
        }
        save_json(CONFIG_FILE, guild_cfg)
        await interaction.response.send_message(f"✅ Template **{self._name}** saved.", ephemeral=True)


@bot.tree.command(name="template_save", description="Elysian: Save a reusable embed template.")
@app_commands.describe(name="Template name (e.g. Rules, Welcome, Announcement)")
async def template_save(interaction: discord.Interaction, name: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(TemplateModal(name))

@bot.tree.command(name="template_post", description="Elysian: Post a saved embed template.")
@app_commands.describe(name="Template name", channel="Channel to post in (default: current)")
async def template_post(interaction: discord.Interaction, name: str, channel: discord.TextChannel = None):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    cfg  = gcfg(interaction.guild.id)
    tmpl = cfg.get("templates", {}).get(name)
    if not tmpl:
        return await interaction.response.send_message(f"No template named **{name}**. Use `/template_list` to see all.", ephemeral=True)

    dest = channel or interaction.channel
    try:
        color = int(tmpl.get("color", "7B5EA7"), 16)
    except Exception:
        color = 0x7B5EA7

    em = discord.Embed(
        title=fill_vars(tmpl.get("title", ""), interaction.user),
        description=fill_vars(tmpl.get("description", ""), interaction.user),
        color=color
    )
    if tmpl.get("footer"):
        em.set_footer(text=fill_vars(tmpl["footer"], interaction.user))
    if tmpl.get("image"):
        em.set_image(url=tmpl["image"])

    await dest.send(embed=em)
    await interaction.response.send_message(f"✅ Template **{name}** posted in {dest.mention}.", ephemeral=True)

@bot.tree.command(name="template_list", description="Elysian: List all saved embed templates.")
async def template_list(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    templates = gcfg(interaction.guild.id).get("templates", {})
    if not templates:
        return await interaction.response.send_message("No templates saved yet. Use `/template_save` to create one.", ephemeral=True)
    em = discord.Embed(title="📚 Saved Templates", color=0x7B5EA7)
    for name, t in templates.items():
        em.add_field(name=f"• {name}", value=t.get("title", "*(no title)*")[:80], inline=False)
    em.set_footer(text="Use /template_post [name] to post one.")
    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="template_delete", description="Elysian: Delete a saved template.")
@app_commands.describe(name="Template name to delete")
async def template_delete(interaction: discord.Interaction, name: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    cfg = gcfg(interaction.guild.id)
    if name not in cfg.get("templates", {}):
        return await interaction.response.send_message(f"No template named **{name}**.", ephemeral=True)
    del cfg["templates"][name]
    save_json(CONFIG_FILE, guild_cfg)
    await interaction.response.send_message(f"🗑️ Template **{name}** deleted.", ephemeral=True)

# ─── AI SUMMARY ───────────────────────────────────────────────────────────────

@bot.tree.command(name="summarize", description="Elysian: Summarize a long text into 3 key points using AI.")
@app_commands.describe(text="The text or article to summarize")
async def summarize(interaction: discord.Interaction, text: str):
    if not gemini_model:
        return await interaction.response.send_message(
            "🔮 The AI oracle is not yet awakened. Please add a `GEMINI_API_KEY` secret to activate this feature.",
            ephemeral=True
        )
    await interaction.response.defer()
    try:
        prompt = (
            f"Summarize the following text into exactly 3 concise bullet points. "
            f"Be clear, helpful, and academic in tone:\n\n{text[:4000]}"
        )
        response = gemini_model.generate_content(prompt)
        summary  = response.text.strip()
        em = discord.Embed(title="🔮 AI Summary", description=summary, color=0x7B5EA7)
        em.set_footer(text="Elysian • Powered by Gemini AI")
        await interaction.followup.send(embed=em)
    except Exception as e:
        await interaction.followup.send(f"The oracle encountered an error: `{e}`", ephemeral=True)

# ─── EMBED BUILDER ────────────────────────────────────────────────────────────

class EmbedBuilderModal(discord.ui.Modal, title="✨ Elysian Embed Builder"):
    e_title  = discord.ui.TextInput(label="Title", placeholder="Enter a title...", max_length=256)
    e_desc   = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                    placeholder="Write your message... Supports {user.name} etc.", max_length=2048)
    e_color  = discord.ui.TextInput(label="Color (hex, e.g. 7B5EA7)", placeholder="7B5EA7", max_length=6, required=False)
    e_footer = discord.ui.TextInput(label="Footer Text", placeholder="Optional...", required=False, max_length=200)
    e_image  = discord.ui.TextInput(label="Image URL (optional)", placeholder="https://...", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = int(self.e_color.value.strip().lstrip("#") or "7B5EA7", 16)
        except ValueError:
            color = 0x7B5EA7
        em = discord.Embed(
            title=fill_vars(self.e_title.value, interaction.user),
            description=fill_vars(self.e_desc.value, interaction.user),
            color=color
        )
        if self.e_footer.value:
            em.set_footer(text=fill_vars(self.e_footer.value, interaction.user))
        if self.e_image.value.strip():
            em.set_image(url=self.e_image.value.strip())
        await interaction.response.send_message(embed=em)

@bot.tree.command(name="embed", description="Elysian: Build a beautiful custom embed via pop-up form.")
async def embed_builder(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(EmbedBuilderModal())

# ─── SHOP ─────────────────────────────────────────────────────────────────────

class ShopDropdown(discord.ui.Select):
    def __init__(self, items):
        options = [
            discord.SelectOption(
                label=item["name"][:25],
                description=f"{item['price']} Ink — {item.get('description','')[:50]}",
                value=str(i), emoji="💎"
            )
            for i, item in enumerate(items)
        ]
        super().__init__(placeholder="✦ Browse the boutique...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        idx  = int(self.values[0])
        item = shop_db["items"][idx]
        em = discord.Embed(title=f"💎 {item['name']}", description=item.get("description",""), color=0x7B5EA7)
        em.add_field(name="Price", value=f"{item['price']} Ink", inline=True)
        role = interaction.guild.get_role(item.get("role_id", 0))
        em.add_field(name="Reward", value=role.mention if role else "Special Perk", inline=True)
        em.set_footer(text=f"Use /buy {item['name']} to purchase.")
        await interaction.response.send_message(embed=em, ephemeral=True)

class ShopView(discord.ui.View):
    def __init__(self, items):
        super().__init__(timeout=60)
        self.add_item(ShopDropdown(items))

@bot.tree.command(name="shop", description="Elysian: Browse the boutique and spend your Ink.")
async def shop(interaction: discord.Interaction):
    items = shop_db.get("items", [])
    if not items:
        return await interaction.response.send_message(
            "The boutique is empty. The Architect has not yet stocked the shelves.", ephemeral=True
        )
    em = discord.Embed(
        title="🛍️ The Elysian Boutique",
        description="Select an item below to view details. Use `/buy [name]` to purchase.",
        color=0x7B5EA7
    )
    em.set_footer(text="Elysian Prestige System • Powered by Ink")
    await interaction.response.send_message(embed=em, view=ShopView(items))

@bot.tree.command(name="buy", description="Elysian: Purchase an item from the boutique.")
@app_commands.describe(item_name="Name of the item to purchase")
async def buy(interaction: discord.Interaction, item_name: str):
    item = next((i for i in shop_db.get("items", []) if i["name"].lower() == item_name.lower()), None)
    if not item:
        return await interaction.response.send_message(f"No item named **{item_name}**. Check `/shop`.", ephemeral=True)
    data = get_user(str(interaction.user.id))
    if data["ink"] < item["price"]:
        return await interaction.response.send_message(
            f"Not enough Ink. You have **{data['ink']}**, need **{item['price']}**.", ephemeral=True
        )
    data["ink"] -= item["price"]
    save_json(USERS_FILE, users_db)
    role = interaction.guild.get_role(item.get("role_id", 0))
    if role:
        try:
            await interaction.user.add_roles(role, reason="Elysian Shop purchase")
        except Exception:
            pass
    em = discord.Embed(title="✅ Purchase Complete", description=f"You acquired **{item['name']}**.", color=0x57f287)
    em.add_field(name="💧 Remaining Ink", value=f"{data['ink']:,}")
    if role:
        em.add_field(name="🎭 Role Granted", value=role.mention)
    await interaction.response.send_message(embed=em, ephemeral=True)

@bot.tree.command(name="admin_add_item", description="Elysian: Add a new item to the boutique.")
@app_commands.describe(name="Item name", price="Ink price", role="Role granted on purchase", description="Short description")
async def admin_add_item(interaction: discord.Interaction, name: str, price: int, role: discord.Role, description: str = ""):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    shop_db["items"].append({"name": name, "price": price, "role_id": role.id, "description": description})
    save_json(SHOP_FILE, shop_db)
    await interaction.response.send_message(
        f"✅ **{name}** added for **{price} Ink** → {role.mention}.", ephemeral=True
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if not TOKEN:
    print("ERROR: DISCORD_TOKEN is not set.")
    exit(1)

bot.run(TOKEN)
