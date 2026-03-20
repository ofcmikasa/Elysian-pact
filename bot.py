import discord
from discord.ext import commands
from discord import app_commands
import os

TOKEN = os.environ.get("DISCORD_TOKEN")
VAULT_CHANNEL_ID = 1484495219176243200
OWNER_ID = 1456572804815261858

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

bot = Elysian()

@bot.event
async def on_message_delete(message):
    if message.author.bot:
        return

    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault is None:
        return

    embed = discord.Embed(title="Message Deleted", color=0xff4d4d)
    embed.add_field(name="Scholar", value=f"{message.author.mention} ({message.author.id})", inline=False)
    embed.add_field(name="Location", value=message.channel.mention, inline=False)
    embed.add_field(name="Content", value=message.content or "*(Attachment or Embed)*", inline=False)
    embed.set_footer(text="Elysian Vault Security")

    await vault.send(embed=embed)

@bot.event
async def on_message_edit(before, after):
    if before.author.bot or before.content == after.content:
        return

    vault = bot.get_channel(VAULT_CHANNEL_ID)
    if vault is None:
        return

    embed = discord.Embed(title="Message Edited", color=0xffcc00)
    embed.add_field(name="Scholar", value=before.author.mention, inline=False)
    embed.add_field(name="Original", value=before.content or "*(empty)*", inline=False)
    embed.add_field(name="Revised", value=after.content or "*(empty)*", inline=False)
    embed.set_footer(text="Elysian Vault Security")

    await vault.send(embed=embed)

@bot.tree.command(name="purge", description="Elysian: Cleanse the chat ripples.")
@app_commands.describe(amount="How many messages to remove?")
async def purge(interaction: discord.Interaction, amount: int):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message(
            "Only the Architect may use this command.", ephemeral=True
        )

    await interaction.response.send_message(
        f"Purifying {amount} messages...", ephemeral=True
    )
    await interaction.channel.purge(limit=amount)

@bot.tree.command(name="lock", description="Elysian: Close the library gates.")
async def lock(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Access Denied.", ephemeral=True)

    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)

    embed = discord.Embed(
        description="The gates are closed. Silence requested for deep work.",
        color=0x2f3136
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unlock", description="Elysian: Open the library gates.")
async def unlock(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Access Denied.", ephemeral=True)

    overwrite = interaction.channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = True
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=overwrite)

    embed = discord.Embed(
        description="The gates are open. The library welcomes all scholars.",
        color=0x57f287
    )
    await interaction.response.send_message(embed=embed)

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable is not set.")
    exit(1)

bot.run(TOKEN)
