import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import time
import unicodedata
from datetime import timedelta
from collections import defaultdict

TOKEN = os.environ.get("DISCORD_TOKEN")
VAULT_CHANNEL_ID = 1484495219176243200
OWNER_ID = 1456572804815261858

WARNS_FILE = "warns.json"

def load_warns():
    if os.path.exists(WARNS_FILE):
        with open(WARNS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_warns(data):
    with open(WARNS_FILE, "w") as f:
        json.dump(data, f, indent=2)

warns_db = load_warns()
invite_cache = {}
message_tracker = defaultdict(list)
study_warn_cooldown = {}


def clean_nickname(name: str) -> str:
    cleaned = ""
    for char in name:
        cat = unicodedata.category(char)
        if cat.startswith("C") or cat == "Cf":
            continue
        cleaned += char
    cleaned = cleaned.strip()
    return cleaned if cleaned else "Scholar"


class Elysian(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="e!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("✧ Elysian is online. Guardian of the Library is active. ✧")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("------")
        for guild in self.guilds:
            try:
                invites = await guild.fetch_invites()
                invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
            except Exception:
                pass


bot = Elysian()


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


async def vault_log(embed: discord.Embed):
    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault:
        await vault.send(embed=embed)


# ─── VAULT: EVENT LISTENERS ───────────────────────────────────────────────────

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

    # Study Warden: nudge users who chat too much (10+ msgs in 60 seconds)
    message_tracker[uid].append(now)
    message_tracker[uid] = [t for t in message_tracker[uid] if now - t < 60]
    if len(message_tracker[uid]) >= 10:
        last_warned = study_warn_cooldown.get(uid, 0)
        if now - last_warned > 600:
            study_warn_cooldown[uid] = now
            message_tracker[uid] = []
            try:
                await message.author.send(
                    "📚 *Scholar, your books are waiting.* You've been quite active in the channels. "
                    "Shall I mute this channel for you so you can focus?"
                )
            except Exception:
                pass


@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return
    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault is None:
        return

    # Ghost Ping: deleted message that mentioned someone
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

    # Vanished Media: re-post deleted images as evidence
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
    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault is None:
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
        vault = bot.get_channel(VAULT_CHANNEL_ID)
        if vault:
            added = [r for r in after.roles if r not in before.roles]
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

    # Auto-Nickname: clean invisible/control characters
    cleaned = clean_nickname(after.display_name)
    if cleaned != after.display_name:
        try:
            await after.edit(nick=cleaned, reason="Elysian Auto-Nickname: cleaned special characters")
        except Exception:
            pass


@bot.event
async def on_member_join(member):
    # Auto-Nickname on join
    cleaned = clean_nickname(member.display_name)
    if cleaned != member.display_name:
        try:
            await member.edit(nick=cleaned, reason="Elysian Auto-Nickname: cleaned special characters")
        except Exception:
            pass

    # Invite Watch
    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault is None:
        return
    try:
        new_invites = await member.guild.fetch_invites()
        new_invite_map = {inv.code: inv.uses for inv in new_invites}
        used_invite = None
        for code, uses in new_invite_map.items():
            old_uses = invite_cache.get(member.guild.id, {}).get(code, 0)
            if uses > old_uses:
                used_invite = next((inv for inv in new_invites if inv.code == code), None)
                break
        invite_cache[member.guild.id] = new_invite_map

        embed = discord.Embed(title="🔗 Invite Watch — New Member", color=0x5865f2)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Member", value=f"{member.mention} (`{member.id}`)", inline=False)
        if used_invite:
            inviter = used_invite.inviter.mention if used_invite.inviter else "Unknown"
            embed.add_field(name="Invite Code", value=f"`{used_invite.code}`", inline=True)
            embed.add_field(name="Created By", value=inviter, inline=True)
            embed.add_field(name="Total Uses", value=str(used_invite.uses), inline=True)
        else:
            embed.add_field(name="Invite", value="Could not determine which link was used.", inline=False)
        embed.set_footer(text="Elysian Vault • Invite Watch")
        await vault.send(embed=embed)
    except Exception:
        pass


# ─── CLEANSE COMMANDS ─────────────────────────────────────────────────────────

@bot.tree.command(name="purge", description="Elysian: Delete a number of recent messages.")
@app_commands.describe(amount="Number of messages to delete")
async def purge(interaction: discord.Interaction, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Cleansed **{len(deleted)}** messages from the archive.", ephemeral=True)


@bot.tree.command(name="purge_user", description="Elysian: Delete messages from a specific user.")
@app_commands.describe(user="The user whose messages to remove", amount="Messages to scan")
async def purge_user(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount, check=lambda m: m.author.id == user.id)
    await interaction.followup.send(
        f"Removed **{len(deleted)}** messages from **{user.display_name}**.", ephemeral=True
    )


@bot.tree.command(name="nuke", description="Elysian: Wipe and recreate this channel entirely.")
async def nuke(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    new_channel = await channel.clone(reason="Elysian Nuke — channel purified")
    await new_channel.edit(position=channel.position)
    await channel.delete(reason="Elysian Nuke")
    await new_channel.send(
        "🌌 *The library has been purified. A new chapter begins.*",
        delete_after=12
    )


@bot.tree.command(name="slowmode", description="Elysian: Set slowmode delay to throttle the chat.")
@app_commands.describe(seconds="Delay in seconds (0 to disable)")
async def slowmode(interaction: discord.Interaction, seconds: int):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        embed = discord.Embed(description="⏱️ Slowmode lifted. The flow of time is restored.", color=0x57f287)
    else:
        embed = discord.Embed(
            description=f"⏱️ Slowmode set to **{seconds}s**. The library demands patience.",
            color=0xffa500
        )
    await interaction.response.send_message(embed=embed)


# ─── SILENCE COMMANDS ─────────────────────────────────────────────────────────

@bot.tree.command(name="mute", description="Elysian: Silence a scholar with a timeout.")
@app_commands.describe(user="The user to mute", minutes="Duration in minutes", reason="Reason for silence")
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


@bot.tree.command(name="warn", description="Elysian: Add a strike to a scholar's permanent record.")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    uid = str(user.id)
    if uid not in warns_db:
        warns_db[uid] = {"username": str(user), "warnings": []}
    warns_db[uid]["warnings"].append({
        "reason": reason,
        "timestamp": discord.utils.utcnow().isoformat()
    })
    save_warns(warns_db)
    count = len(warns_db[uid]["warnings"])
    embed = discord.Embed(
        description=f"📜 **{user.display_name}** has received a strike. Total: **{count}** warning(s).\nReason: *{reason}*",
        color=0xffa500
    )
    await interaction.response.send_message(embed=embed)
    try:
        await user.send(
            f"⚠️ You have received a warning in **{interaction.guild.name}**.\nReason: **{reason}**"
        )
    except Exception:
        pass


@bot.tree.command(name="warnings", description="Elysian: View a scholar's full warning record.")
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
@app_commands.describe(user="The user to kick", reason="Reason for removal")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    try:
        await user.send(
            f"👣 You have been removed from **{interaction.guild.name}**.\nReason: **{reason}**"
        )
    except Exception:
        pass
    await user.kick(reason=reason)
    embed = discord.Embed(
        description=f"👣 **{user.display_name}** has been escorted from the library.\nReason: *{reason}*",
        color=0xed4245
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ban", description="Elysian: Permanently exile a scholar.")
@app_commands.describe(user="The user to ban", reason="Reason for exile")
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    try:
        await user.send(
            f"🔒 You have been permanently exiled from **{interaction.guild.name}**.\nReason: **{reason}**"
        )
    except Exception:
        pass
    await user.ban(reason=reason, delete_message_days=0)
    embed = discord.Embed(
        description=f"🔒 **{user.display_name}** has been exiled from the library.\nReason: *{reason}*",
        color=0xed4245
    )
    await interaction.response.send_message(embed=embed)


# ─── FORTRESS COMMANDS ────────────────────────────────────────────────────────

@bot.tree.command(name="lock", description="Elysian: Freeze the current channel.")
async def lock(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        description="🔒 *The gates are sealed. This chamber demands silence.*",
        color=0x2f3136
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="lockdown_server", description="Elysian: Freeze every public channel at once.")
async def lockdown_server(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    locked = 0
    for channel in interaction.guild.text_channels:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        if overwrite.send_messages is not False:
            overwrite.send_messages = False
            try:
                await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
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
    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = True
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        description="🔓 *The gates are open. The library welcomes all scholars.*",
        color=0x57f287
    )
    await interaction.response.send_message(embed=embed)


# ─── PRESTIGE: EMBED BUILDER ──────────────────────────────────────────────────

class EmbedBuilderModal(discord.ui.Modal, title="✨ Elysian Embed Builder"):
    embed_title = discord.ui.TextInput(
        label="Title", placeholder="Enter a title...", max_length=256
    )
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


@bot.tree.command(name="embed", description="Elysian: Build a beautiful custom embed via a pop-up form.")
async def embed_builder(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await interaction.response.send_message("Access denied.", ephemeral=True)
    await interaction.response.send_modal(EmbedBuilderModal())


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
    exit(1)

bot.run(TOKEN)
