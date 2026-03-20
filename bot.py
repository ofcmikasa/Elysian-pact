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

TOKEN = os.environ.get("DISCORD_TOKEN")
OWNER_ID = 1456572804815261858

# ─── DATA FILES ───────────────────────────────────────────────────────────────

WARNS_FILE   = "warns.json"
USERS_FILE   = "users.json"
SHOP_FILE    = "shop.json"
CONFIG_FILE  = "guild_config.json"
SESSIONS_FILE = "sessions.json"

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

warns_db    = load_json(WARNS_FILE,   {})
users_db    = load_json(USERS_FILE,   {})
shop_db     = load_json(SHOP_FILE,    {"items": []})
guild_cfg   = load_json(CONFIG_FILE,  {})
sessions_db = load_json(SESSIONS_FILE, {})  # uid -> {start, topic}

invite_cache       = {}
message_tracker    = defaultdict(list)
study_warn_cd      = {}
voice_join_times   = {}   # uid -> unix timestamp of VC join

# ─── ROLE TIERS ───────────────────────────────────────────────────────────────

HOUR_TIERS = [
    (100, "100h Immortal"),
    (50,  "50h Sage"),
    (10,  "10h Scholar"),
]
TIER_NAMES = {name for _, name in HOUR_TIERS}

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

def vault_channel_id(guild_id: int) -> int:
    cfg = guild_cfg.get(str(guild_id), {})
    return cfg.get("vault_channel_id", 1484495219176243200)

def study_vc_ids(guild_id: int) -> list:
    cfg = guild_cfg.get(str(guild_id), {})
    return cfg.get("study_vc_ids", [])

def clean_nickname(name: str) -> str:
    cleaned = "".join(
        c for c in name
        if not (unicodedata.category(c).startswith("C") or unicodedata.category(c) == "Cf")
    ).strip()
    return cleaned or "Scholar"

async def get_vault(guild: discord.Guild) -> discord.TextChannel | None:
    cid = vault_channel_id(guild.id)
    return guild.get_channel(cid)

async def auto_role(member: discord.Member, hours: float):
    """Promote/demote member based on total study hours."""
    earned = None
    for required, name in HOUR_TIERS:
        if hours >= required:
            earned = name
            break
    if not earned:
        return

    # Check if they already have this role
    if discord.utils.get(member.roles, name=earned):
        return

    guild = member.guild
    # Remove old tier roles
    for _, name in HOUR_TIERS:
        old = discord.utils.get(guild.roles, name=name)
        if old and old in member.roles:
            try:
                await member.remove_roles(old, reason="Elysian tier progression")
            except Exception:
                pass

    # Add new role
    new_role = discord.utils.get(guild.roles, name=earned)
    if new_role:
        try:
            await member.add_roles(new_role, reason="Elysian tier progression")
        except Exception:
            return

        # Celebration embed
        cfg = guild_cfg.get(str(guild.id), {})
        celebrate_cid = cfg.get("leaderboard_channel_id") or cfg.get("vault_channel_id")
        channel = guild.get_channel(celebrate_cid) if celebrate_cid else None
        if channel:
            colors = {"10h Scholar": 0xC0C0C0, "50h Sage": 0x4169E1, "100h Immortal": 0xFFD700}
            embed = discord.Embed(
                title="✨ Tier Ascension",
                description=(
                    f"**{member.mention}** has ascended to **{new_role.mention}**!\n"
                    f"*The library acknowledges your dedication.*"
                ),
                color=colors.get(earned, 0x7B5EA7)
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Elysian Prestige System")
            await channel.send(embed=embed)

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
        """Every hour, award 10 Ink to users currently in a study VC."""
        now = time.time()
        for uid, join_time in list(voice_join_times.items()):
            user_data = get_user(uid)
            user_data["ink"] += 10
            user_data["total_hours"] = round(user_data.get("total_hours", 0) + 1, 2)
        save_json(USERS_FILE, users_db)

    @passive_ink_task.before_loop
    async def before_passive_ink(self):
        await self.wait_until_ready()

bot = Elysian()

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID

# ─── VAULT LISTENERS ──────────────────────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)

    now = time.time()
    uid = str(message.author.id)

    # Anti-Spam Shield: 5+ images in a single message
    image_count = sum(
        1 for a in message.attachments
        if any(a.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])
    )
    if image_count >= 5:
        try:
            await message.delete()
            await message.channel.send(
                f"{message.author.mention} The Gallery is full. Please wait a moment.",
                delete_after=8
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

    if message.mentions:
        pinged = ", ".join(m.mention for m in message.mentions)
        embed = discord.Embed(title="👻 Ghost Ping Detected", color=0xff6b6b)
        embed.add_field(name="Pinger", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Pinged", value=pinged, inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=False)
        embed.add_field(name="Deleted Message", value=message.content or "*(no text)*", inline=False)
        embed.set_footer(text="Elysian Vault • Ghost Ping")
        await vault.send(embed=embed)
    else:
        embed = discord.Embed(title="🗑️ Message Deleted", color=0xff4d4d)
        embed.add_field(name="Scholar", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Location", value=message.channel.mention, inline=False)
        embed.add_field(name="Content", value=message.content or "*(attachment only)*", inline=False)
        embed.set_footer(text="Elysian Vault • Deleted Message")
        await vault.send(embed=embed)

    for attachment in message.attachments:
        if any(attachment.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            media_embed = discord.Embed(
                title="📷 Vanished Media Recovered",
                description=f"Deleted by {message.author.mention} in {message.channel.mention}",
                color=0xffa500
            )
            media_embed.set_image(url=attachment.url)
            media_embed.set_footer(text="Elysian Vault • Vanished Media")
            await vault.send(embed=media_embed)


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content:
        return
    vault = await get_vault(before.guild)
    if not vault:
        return
    embed = discord.Embed(title="✏️ Shadow Edit Detected", color=0xffcc00)
    embed.add_field(name="Scholar", value=before.author.mention, inline=False)
    embed.add_field(name="Original", value=before.content or "*(empty)*", inline=False)
    embed.add_field(name="Revised", value=after.content or "*(empty)*", inline=False)
    embed.add_field(name="Channel", value=before.channel.mention, inline=False)
    embed.set_footer(text="Elysian Vault • Shadow Edit")
    await vault.send(embed=embed)


@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        vault = await get_vault(after.guild)
        if vault:
            added   = [r for r in after.roles  if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            if added:
                embed = discord.Embed(title="🎭 Role Stealth — Role Added", color=0x57f287)
                embed.add_field(name="Member", value=after.mention, inline=False)
                embed.add_field(name="Role Added", value=", ".join(r.mention for r in added), inline=False)
                embed.set_footer(text="Elysian Vault • Role Stealth")
                await vault.send(embed=embed)
            if removed:
                embed = discord.Embed(title="🎭 Role Stealth — Role Removed", color=0xed4245)
                embed.add_field(name="Member", value=after.mention, inline=False)
                embed.add_field(name="Role Removed", value=", ".join(r.mention for r in removed), inline=False)
                embed.set_footer(text="Elysian Vault • Role Stealth")
                await vault.send(embed=embed)

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

    vault = await get_vault(member.guild)
    if not vault:
        return
    try:
        new_invites = await member.guild.fetch_invites()
        new_map = {inv.code: inv.uses for inv in new_invites}
        used = None
        for code, uses in new_map.items():
            if uses > invite_cache.get(member.guild.id, {}).get(code, 0):
                used = next((inv for inv in new_invites if inv.code == code), None)
                break
        invite_cache[member.guild.id] = new_map

        embed = discord.Embed(title="🔗 Invite Watch — New Member", color=0x5865f2)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        if used:
            inviter = used.inviter.mention if used.inviter else "Unknown"
            embed.add_field(name="Invite Code",  value=f"`{used.code}`", inline=True)
            embed.add_field(name="Created By",   value=inviter,          inline=True)
            embed.add_field(name="Total Uses",   value=str(used.uses),   inline=True)
        else:
            embed.add_field(name="Invite", value="Could not determine.", inline=False)
        embed.set_footer(text="Elysian Vault • Invite Watch")
        await vault.send(embed=embed)
    except Exception:
        pass


@bot.event
async def on_voice_state_update(member, before, after):
    uid = str(member.id)
    cfg = guild_cfg.get(str(member.guild.id), {})
    study_vcs = cfg.get("study_vc_ids", [])

    # Joined a study VC
    if after.channel and after.channel.id in study_vcs and (not before.channel or before.channel.id not in study_vcs):
        voice_join_times[uid] = time.time()

    # Left a study VC
    if before.channel and before.channel.id in study_vcs and (not after.channel or after.channel.id not in study_vcs):
        join_time = voice_join_times.pop(uid, None)
        if join_time:
            elapsed_hours = (time.time() - join_time) / 3600
            ink_earned = int(elapsed_hours * 10)
            user_data = get_user(uid)
            user_data["ink"] += ink_earned
            user_data["total_hours"] = round(user_data.get("total_hours", 0) + elapsed_hours, 2)

            # Streak check
            today = datetime.now(timezone.utc).date().isoformat()
            last = user_data.get("last_study_date")
            if last:
                yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
                if last == yesterday:
                    user_data["streak"] = user_data.get("streak", 0) + 1
                elif last != today:
                    user_data["streak"] = 1
            else:
                user_data["streak"] = 1
            user_data["last_study_date"] = today

            # Streak bonus every 3rd consecutive day
            if user_data["streak"] % 3 == 0:
                user_data["ink"] += 5

            save_json(USERS_FILE, users_db)
            await auto_role(member, user_data["total_hours"])


# ─── CLEANSE COMMANDS ─────────────────────────────────────────────────────────

@bot.tree.command(name="purge", description="Elysian: Delete a number of recent messages.")
@app_commands.describe(amount="Number of messages to delete")
async def purge(interaction: discord.Interaction, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Cleansed **{len(deleted)}** messages.", ephemeral=True)


@bot.tree.command(name="purge_user", description="Elysian: Delete messages from a specific user.")
@app_commands.describe(user="The user to cleanse", amount="Messages to scan")
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
    channel = interaction.channel
    new_ch = await channel.clone(reason="Elysian Nuke")
    await new_ch.edit(position=channel.position)
    await channel.delete(reason="Elysian Nuke")
    await new_ch.send("🌌 *The library has been purified. A new chapter begins.*", delete_after=12)


@bot.tree.command(name="slowmode", description="Elysian: Set slowmode delay.")
@app_commands.describe(seconds="Delay in seconds (0 to disable)")
async def slowmode(interaction: discord.Interaction, seconds: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds)
    msg = "⏱️ Slowmode lifted. The flow of time is restored." if seconds == 0 else f"⏱️ Slowmode set to **{seconds}s**."
    await interaction.response.send_message(embed=discord.Embed(description=msg, color=0xffa500))


# ─── SILENCE COMMANDS ─────────────────────────────────────────────────────────

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
        embed = discord.Embed(
            description=f"🌿 {user.mention} has entered the Silent Gardens for **{minutes}m**.\nReason: *{reason}*",
            color=0x7b5ea7
        )
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message("I lack the authority to silence this scholar.", ephemeral=True)


@bot.tree.command(name="warn", description="Elysian: Add a strike to a scholar's record.")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    uid = str(user.id)
    if uid not in warns_db:
        warns_db[uid] = {"username": str(user), "warnings": []}
    warns_db[uid]["warnings"].append({"reason": reason, "timestamp": discord.utils.utcnow().isoformat()})
    save_json(WARNS_FILE, warns_db)
    count = len(warns_db[uid]["warnings"])
    embed = discord.Embed(
        description=f"📜 **{user.display_name}** received a strike. Total: **{count}**.\nReason: *{reason}*",
        color=0xffa500
    )
    await interaction.response.send_message(embed=embed)
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
            f"📖 **{user.display_name}**'s record is pristine. No strikes found.", ephemeral=True
        )
    data = warns_db[uid]["warnings"]
    embed = discord.Embed(
        title=f"📜 Scholar Record — {user.display_name}",
        description=f"**{len(data)}** warning(s) on record",
        color=0xffa500
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    for i, w in enumerate(data, 1):
        ts = w.get("timestamp", "")[:10]
        embed.add_field(name=f"Strike #{i} — {ts}", value=w["reason"], inline=False)
    embed.set_footer(text="Elysian Vault • Scholar Record")
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    await interaction.response.send_message(
        embed=discord.Embed(description=f"👣 **{user.display_name}** escorted out. Reason: *{reason}*", color=0xed4245)
    )


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
    await interaction.response.send_message(
        embed=discord.Embed(description=f"🔒 **{user.display_name}** exiled. Reason: *{reason}*", color=0xed4245)
    )


# ─── FORTRESS COMMANDS ────────────────────────────────────────────────────────

@bot.tree.command(name="lock", description="Elysian: Freeze the current channel.")
async def lock(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(
        embed=discord.Embed(description="🔒 *The gates are sealed. This chamber demands silence.*", color=0x2f3136)
    )


@bot.tree.command(name="lockdown_server", description="Elysian: Freeze every public channel at once.")
async def lockdown_server(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    locked = 0
    targets = [
        c for c in interaction.guild.channels
        if isinstance(c, (discord.TextChannel, discord.CategoryChannel))
    ]
    for channel in targets:
        ow = channel.overwrites_for(interaction.guild.default_role)
        ow.send_messages = False
        try:
            await channel.set_permissions(interaction.guild.default_role, overwrite=ow)
            locked += 1
        except Exception:
            pass
    await interaction.followup.send(
        f"🔒 Fortress protocol activated. **{locked}** channels sealed.", ephemeral=True
    )


@bot.tree.command(name="unlock", description="Elysian: Restore the flow to the current channel.")
async def unlock(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = True
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message(
        embed=discord.Embed(description="🔓 *The gates are open. The library welcomes all scholars.*", color=0x57f287)
    )


# ─── GENESIS: AUTO-SETUP ──────────────────────────────────────────────────────

@bot.tree.command(name="setup_elysian", description="Elysian: One-time server setup — creates all roles, channels, and categories.")
async def setup_elysian(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    cfg = guild_cfg.setdefault(str(guild.id), {})

    # ── Create category
    category = discord.utils.get(guild.categories, name="✧ ELYSIAN PRESTIGE ✧")
    if not category:
        category = await guild.create_category("✧ ELYSIAN PRESTIGE ✧")

    # ── Create roles
    role_specs = [
        ("10h Scholar",               discord.Color.from_str("#C0C0C0")),
        ("50h Sage",                  discord.Color.from_str("#4169E1")),
        ("100h Immortal",             discord.Color.from_str("#FFD700")),
        ("Library VIP",               discord.Color.from_str("#9B59B6")),
        ("The Architect's Favorite",  discord.Color.from_str("#FF8C00")),
        ("Elite Monthly Rank",        discord.Color.from_str("#FF69B4")),
    ]
    created_roles = {}
    for name, color in role_specs:
        existing = discord.utils.get(guild.roles, name=name)
        if not existing:
            existing = await guild.create_role(name=name, color=color, reason="Elysian Genesis Setup")
        created_roles[name] = existing

    # ── Create vault channel (owner-only)
    vault_ch = discord.utils.get(guild.text_channels, name="vault-logs")
    if not vault_ch:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        owner = guild.get_member(OWNER_ID)
        if owner:
            overwrites[owner] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
        vault_ch = await guild.create_text_channel(
            "vault-logs", category=category, overwrites=overwrites, reason="Elysian Genesis Setup"
        )
    cfg["vault_channel_id"] = vault_ch.id

    # ── Create shop channel
    shop_ch = discord.utils.get(guild.text_channels, name="elysian-shop")
    if not shop_ch:
        shop_ch = await guild.create_text_channel(
            "elysian-shop", category=category, reason="Elysian Genesis Setup"
        )
    cfg["shop_channel_id"] = shop_ch.id

    # ── Create leaderboard channel
    lb_ch = discord.utils.get(guild.text_channels, name="leaderboard-hall")
    if not lb_ch:
        lb_ch = await guild.create_text_channel(
            "leaderboard-hall", category=category, reason="Elysian Genesis Setup"
        )
    cfg["leaderboard_channel_id"] = lb_ch.id

    # ── Create Study VC
    study_vc = discord.utils.get(guild.voice_channels, name="📚 Study Hall")
    if not study_vc:
        study_vc = await guild.create_voice_channel(
            "📚 Study Hall", category=category, reason="Elysian Genesis Setup"
        )
    cfg.setdefault("study_vc_ids", [])
    if study_vc.id not in cfg["study_vc_ids"]:
        cfg["study_vc_ids"].append(study_vc.id)

    save_json(CONFIG_FILE, guild_cfg)

    embed = discord.Embed(
        title="✧ Elysian Genesis Complete ✧",
        description="The library has been established. All chambers are ready.",
        color=0x7B5EA7
    )
    embed.add_field(name="📂 Category",    value="✧ ELYSIAN PRESTIGE ✧", inline=False)
    embed.add_field(name="🎭 Roles",        value="\n".join(f"• {n}" for n, _ in role_specs), inline=True)
    embed.add_field(name="📋 Channels",    value=f"• {vault_ch.mention}\n• {shop_ch.mention}\n• {lb_ch.mention}", inline=True)
    embed.add_field(name="🎙️ Study VC",   value=study_vc.mention, inline=False)
    embed.set_footer(text="Elysian Prestige System")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─── ARCHITECT COMMANDS ───────────────────────────────────────────────────────

@bot.tree.command(name="set_ink", description="Elysian: Manually set a user's Ink balance.")
@app_commands.describe(user="The scholar", amount="Amount of Ink to set")
async def set_ink(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    data = get_user(str(user.id))
    data["ink"] = amount
    save_json(USERS_FILE, users_db)
    await interaction.response.send_message(
        f"💧 **{user.display_name}**'s Ink set to **{amount}**.", ephemeral=True
    )


@bot.tree.command(name="broadcast", description="Elysian: Send a styled announcement to all channels.")
@app_commands.describe(message="The message to broadcast")
async def broadcast(interaction: discord.Interaction, message: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="📣 Elysian Announcement",
        description=message,
        color=0x7B5EA7
    )
    embed.set_footer(text="— The Architect")

    sent = 0
    cfg = guild_cfg.get(str(interaction.guild.id), {})
    vault_id = cfg.get("vault_channel_id", 1484495219176243200)
    for ch in interaction.guild.text_channels:
        if ch.id == vault_id:
            continue
        try:
            await ch.send(embed=embed)
            sent += 1
        except Exception:
            pass

    await interaction.followup.send(f"📣 Broadcast sent to **{sent}** channels.", ephemeral=True)


@bot.tree.command(name="vault_view", description="Elysian: View a summary of recent vault events.")
async def vault_view(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)

    embed = discord.Embed(title="👁️ Vault Summary", color=0x7B5EA7)

    total_warns = sum(len(v.get("warnings", [])) for v in warns_db.values())
    embed.add_field(name="⚠️ Total Warnings Issued", value=str(total_warns), inline=True)
    embed.add_field(name="👥 Scholars on Record",    value=str(len(warns_db)),   inline=True)

    recent = []
    for uid, d in warns_db.items():
        for w in d.get("warnings", []):
            recent.append((d.get("username", uid), w["reason"], w.get("timestamp", "")[:10]))
    recent = sorted(recent, key=lambda x: x[2], reverse=True)[:5]
    if recent:
        warns_text = "\n".join(f"• **{u}** — {r} `({ts})`" for u, r, ts in recent)
        embed.add_field(name="📋 Recent Warnings", value=warns_text, inline=False)
    else:
        embed.add_field(name="📋 Recent Warnings", value="None on record.", inline=False)

    embed.set_footer(text="Elysian Vault • Summary View")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── SCHOLAR COMMANDS ─────────────────────────────────────────────────────────

@bot.tree.command(name="profile", description="Elysian: View your scholar profile card.")
@app_commands.describe(user="Scholar to view (leave blank for yourself)")
async def profile(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    data = get_user(str(target.id))

    # Current tier
    tier = "Unranked"
    for required, name in HOUR_TIERS:
        if data.get("total_hours", 0) >= required:
            tier = name
            break

    tier_colors = {"10h Scholar": 0xC0C0C0, "50h Sage": 0x4169E1, "100h Immortal": 0xFFD700}
    color = tier_colors.get(tier, 0x7B5EA7)

    embed = discord.Embed(title=f"✦ {target.display_name}", color=color)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="💧 Ink",          value=f"{data.get('ink', 0):,}",                  inline=True)
    embed.add_field(name="⏱️ Study Hours",  value=f"{data.get('total_hours', 0):.1f}h",        inline=True)
    embed.add_field(name="🔥 Streak",       value=f"{data.get('streak', 0)} day(s)",            inline=True)
    embed.add_field(name="🎓 Rank",         value=tier,                                         inline=True)

    # Active focus session
    uid = str(target.id)
    if uid in sessions_db:
        sess = sessions_db[uid]
        elapsed = (time.time() - sess["start"]) / 60
        embed.add_field(
            name="📚 Focus Session",
            value=f"*{sess.get('topic', 'Open study')}* — {elapsed:.0f}m active",
            inline=False
        )

    embed.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Elysian: View the top scholars by study hours.")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    if not users_db:
        return await interaction.followup.send("No scholars on record yet.", ephemeral=True)

    sorted_users = sorted(
        [(uid, d) for uid, d in users_db.items()],
        key=lambda x: x[1].get("total_hours", 0),
        reverse=True
    )[:10]

    medals = ["🥇", "🥈", "🥉"] + ["✦"] * 7
    embed = discord.Embed(
        title="🏛️ Elysian Leaderboard — Hall of Scholars",
        description="The most dedicated minds in the library.",
        color=0xFFD700
    )

    for i, (uid, d) in enumerate(sorted_users):
        member = interaction.guild.get_member(int(uid))
        name   = member.display_name if member else f"Scholar #{uid[:6]}"
        hours  = d.get("total_hours", 0)
        ink    = d.get("ink", 0)

        tier = "Unranked"
        for req, tname in HOUR_TIERS:
            if hours >= req:
                tier = tname
                break

        embed.add_field(
            name=f"{medals[i]} #{i+1} — {name}",
            value=f"⏱️ `{hours:.1f}h` • 💧 `{ink:,} Ink` • 🎓 {tier}",
            inline=False
        )

    embed.set_footer(text="Elysian Prestige System • Updated now")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="focus", description="Elysian: Start a focus session and track your time.")
@app_commands.describe(topic="What are you studying?")
async def focus(interaction: discord.Interaction, topic: str = "Open study"):
    uid = str(interaction.user.id)
    if uid in sessions_db:
        elapsed = (time.time() - sessions_db[uid]["start"]) / 60
        return await interaction.response.send_message(
            f"📚 You already have an active session: *{sessions_db[uid]['topic']}* — {elapsed:.0f}m in.\n"
            f"Use `/endfocus` to close it first.",
            ephemeral=True
        )
    sessions_db[uid] = {"start": time.time(), "topic": topic}
    save_json(SESSIONS_FILE, sessions_db)

    embed = discord.Embed(
        title="📚 Focus Session Started",
        description=f"**{interaction.user.display_name}** has entered deep focus.\n*Topic: {topic}*",
        color=0x7B5EA7
    )
    embed.set_footer(text="Use /endfocus to end your session and claim your Ink.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="endfocus", description="Elysian: End your focus session and claim Ink.")
async def endfocus(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in sessions_db:
        return await interaction.response.send_message(
            "You don't have an active focus session. Use `/focus` to start one.", ephemeral=True
        )
    sess = sessions_db.pop(uid)
    save_json(SESSIONS_FILE, sessions_db)

    elapsed_hours = (time.time() - sess["start"]) / 3600
    ink_earned = int(elapsed_hours * 10)

    data = get_user(uid)
    data["ink"] += ink_earned
    data["total_hours"] = round(data.get("total_hours", 0) + elapsed_hours, 2)

    # Streak check
    today = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    last = data.get("last_study_date")
    if last == yesterday:
        data["streak"] = data.get("streak", 0) + 1
    elif last != today:
        data["streak"] = 1
    data["last_study_date"] = today

    if data["streak"] % 3 == 0:
        data["ink"] += 5
        bonus_text = " (+5 streak bonus!)"
    else:
        bonus_text = ""

    save_json(USERS_FILE, users_db)
    await auto_role(interaction.user, data["total_hours"])

    embed = discord.Embed(
        title="✅ Focus Session Complete",
        description=f"*{sess['topic']}*",
        color=0x57f287
    )
    embed.add_field(name="⏱️ Duration",    value=f"{elapsed_hours*60:.0f} minutes",       inline=True)
    embed.add_field(name="💧 Ink Earned",  value=f"{ink_earned}{bonus_text}",              inline=True)
    embed.add_field(name="🔥 Streak",      value=f"{data['streak']} day(s)",               inline=True)
    embed.set_footer(text="Elysian Prestige System")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="daily", description="Elysian: Collect your daily Ink blessing.")
async def daily(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    data = get_user(uid)
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    if data.get("last_daily") == today:
        return await interaction.response.send_message(
            "✨ You've already collected today's blessing. Return tomorrow, scholar.",
            ephemeral=True
        )

    amount = random.randint(5, 20)
    data["ink"] += amount
    data["last_daily"] = today
    save_json(USERS_FILE, users_db)

    embed = discord.Embed(
        title="✨ Daily Blessing",
        description=f"The library bestows **{amount} Ink** upon you, {interaction.user.mention}.",
        color=0xFFD700
    )
    embed.add_field(name="💧 Total Ink", value=f"{data['ink']:,}")
    embed.set_footer(text="Return tomorrow for another blessing.")
    await interaction.response.send_message(embed=embed)


# ─── SHOP SYSTEM ──────────────────────────────────────────────────────────────

class ShopDropdown(discord.ui.Select):
    def __init__(self, items):
        options = [
            discord.SelectOption(
                label=item["name"][:25],
                description=f"{item['price']} Ink — {item.get('description','')[:50]}",
                value=str(i),
                emoji="💎"
            )
            for i, item in enumerate(items)
        ]
        super().__init__(placeholder="✦ Browse the boutique...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        item = shop_db["items"][idx]
        embed = discord.Embed(title=f"💎 {item['name']}", description=item.get("description", ""), color=0x7B5EA7)
        embed.add_field(name="Price", value=f"{item['price']} Ink", inline=True)
        role = interaction.guild.get_role(item.get("role_id", 0))
        embed.add_field(name="Reward", value=role.mention if role else "Special Perk", inline=True)
        embed.set_footer(text=f"Use /buy {item['name']} to purchase.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
    embed = discord.Embed(
        title="🛍️ The Elysian Boutique",
        description="Select an item below to view its details.\nUse `/buy [name]` to purchase.",
        color=0x7B5EA7
    )
    embed.set_footer(text="Elysian Prestige System • Powered by Ink")
    await interaction.response.send_message(embed=embed, view=ShopView(items))


@bot.tree.command(name="buy", description="Elysian: Purchase an item from the boutique.")
@app_commands.describe(item_name="Name of the item to purchase")
async def buy(interaction: discord.Interaction, item_name: str):
    items = shop_db.get("items", [])
    item = next((i for i in items if i["name"].lower() == item_name.lower()), None)
    if not item:
        return await interaction.response.send_message(
            f"No item called **{item_name}** found. Check `/shop` for the menu.", ephemeral=True
        )

    data = get_user(str(interaction.user.id))
    if data["ink"] < item["price"]:
        return await interaction.response.send_message(
            f"Not enough Ink. You have **{data['ink']}**, this costs **{item['price']}**.",
            ephemeral=True
        )

    data["ink"] -= item["price"]
    save_json(USERS_FILE, users_db)

    role = interaction.guild.get_role(item.get("role_id", 0))
    if role:
        try:
            await interaction.user.add_roles(role, reason="Elysian Shop purchase")
        except Exception:
            pass

    embed = discord.Embed(
        title="✅ Purchase Complete",
        description=f"You acquired **{item['name']}**.",
        color=0x57f287
    )
    embed.add_field(name="💧 Remaining Ink", value=f"{data['ink']:,}")
    if role:
        embed.add_field(name="🎭 Role Granted", value=role.mention)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="admin_add_item", description="Elysian: Add a new item to the boutique.")
@app_commands.describe(name="Item name", price="Ink price", role="Role to grant on purchase", description="Short description")
async def admin_add_item(
    interaction: discord.Interaction,
    name: str,
    price: int,
    role: discord.Role,
    description: str = ""
):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)

    shop_db["items"].append({
        "name": name,
        "price": price,
        "role_id": role.id,
        "description": description
    })
    save_json(SHOP_FILE, shop_db)
    await interaction.response.send_message(
        f"✅ **{name}** added to the boutique for **{price} Ink** → {role.mention}.", ephemeral=True
    )


# ─── PRESTIGE: EMBED BUILDER ──────────────────────────────────────────────────

class EmbedBuilderModal(discord.ui.Modal, title="✨ Elysian Embed Builder"):
    embed_title = discord.ui.TextInput(label="Title", placeholder="Enter a title...", max_length=256)
    embed_description = discord.ui.TextInput(
        label="Description", style=discord.TextStyle.paragraph,
        placeholder="Write your message here...", max_length=2048
    )
    embed_color = discord.ui.TextInput(
        label="Color (hex, e.g. 7B5EA7)", placeholder="7B5EA7", max_length=6, required=False
    )
    embed_footer = discord.ui.TextInput(
        label="Footer Text", placeholder="Optional footer...", required=False, max_length=200
    )
    embed_image_url = discord.ui.TextInput(
        label="Image URL (optional)", placeholder="https://...", required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = int(self.embed_color.value.strip().lstrip("#") or "7B5EA7", 16)
        except ValueError:
            color = 0x7B5EA7

        embed = discord.Embed(
            title=self.embed_title.value,
            description=self.embed_description.value,
            color=color
        )
        if self.embed_footer.value:
            embed.set_footer(text=self.embed_footer.value)
        if self.embed_image_url.value.strip():
            embed.set_image(url=self.embed_image_url.value.strip())

        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="embed", description="Elysian: Build a beautiful custom embed via pop-up form.")
async def embed_builder(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(EmbedBuilderModal())


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
    exit(1)

bot.run(TOKEN)
