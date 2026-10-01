import os
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime, timezone
from dotenv import load_dotenv
from anthropic import Anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import traceback

load_dotenv()

# ====================== CONFIG ======================
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
DATABASE = "notes.db"

# Categories
CATEGORIES = [
    "GT",
    "SC",
    "GH",
    "FE",
    "Places",
    "NPCs",
    "General Knowledge",
    "Private Knowledge"
]

# Emoji → Category quick-add map
REACTION_MAP = {
    "🦷": "GT",
    "☀️": "SC",
    "🌞": "SC",      # alternative sun
    "🏹": "GH",
    "✨": "FE",
}

# Timezone for the Sunday job (change if you want)
TIMEZONE = pytz.timezone("Europe/Amsterdam")  # CEST-friendly default

# ====================================================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.members = True  # sometimes needed for reaction events

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


# -------------------- Database --------------------

async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                comment TEXT,
                author_name TEXT,
                author_id INTEGER,
                message_link TEXT,
                channel_name TEXT,
                created_at TEXT NOT NULL,
                added_by INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()


async def add_note(category, content, comment, author_name, author_id, message_link, channel_name, added_by):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            INSERT INTO notes (category, content, comment, author_name, author_id, message_link, channel_name, created_at, added_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            category,
            content,
            comment,
            author_name,
            author_id,
            message_link,
            channel_name,
            datetime.now(timezone.utc).isoformat(),
            added_by
        ))
        await db.commit()
        return cursor.lastrowid


async def get_notes(category=None, search=None, limit=50):
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        if category and search:
            cursor = await db.execute("""
                SELECT * FROM notes
                WHERE category = ? AND (content LIKE ? OR comment LIKE ?)
                ORDER BY id DESC LIMIT ?
            """, (category, f"%{search}%", f"%{search}%", limit))
        elif category:
            cursor = await db.execute("""
                SELECT * FROM notes WHERE category = ?
                ORDER BY id DESC LIMIT ?
            """, (category, limit))
        elif search:
            cursor = await db.execute("""
                SELECT * FROM notes
                WHERE content LIKE ? OR comment LIKE ?
                ORDER BY id DESC LIMIT ?
            """, (f"%{search}%", f"%{search}%", limit))
        else:
            cursor = await db.execute("""
                SELECT * FROM notes ORDER BY id DESC LIMIT ?
            """, (limit,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_note_by_id(note_id):
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_note(note_id, user_id):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("DELETE FROM notes WHERE id = ? AND added_by = ?", (note_id, user_id))
        await db.commit()
        return cursor.rowcount > 0


async def export_all_notes_markdown():
    notes = await get_notes(limit=10000)
    if not notes:
        return "# Your PbP Notes\n\nNo notes yet."

    # Group by category
    grouped = {cat: [] for cat in CATEGORIES}
    grouped["Uncategorized"] = []

    for note in notes:
        cat = note["category"] if note["category"] in grouped else "Uncategorized"
        grouped[cat].append(note)

    md = f"# Your PbP Notes\n\nExported on {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M %Z')}\n\n"

    for cat, items in grouped.items():
        if not items:
            continue
        md += f"## {cat}\n\n"
        for n in items:
            md += f"### Note #{n['id']}\n"
            md += f"- **From:** {n['author_name']} in #{n['channel_name']}\n"
            md += f"- **Date:** {n['created_at'][:10]}\n"
            if n['message_link']:
                md += f"- **Link:** {n['message_link']}\n"
            if n['comment']:
                md += f"- **Your comment:** {n['comment']}\n"
            md += f"\n{n['content']}\n\n---\n\n"

    return md


async def get_last_summary_date():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT value FROM meta WHERE key = 'last_summary'")
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_last_summary_date(date_str):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            INSERT OR REPLACE INTO meta (key, value) VALUES ('last_summary', ?)
        """, (date_str,))
        await db.commit()


# -------------------- AI Summary --------------------

async def generate_summary_with_claude(notes_text: str) -> str:
    if not anthropic_client:
        return "Error: Anthropic API key is missing."

    prompt = f"""You are a helpful assistant for a Play-by-Post (PbP) roleplaying game on Discord.

Here are the notes the player has collected:

{notes_text}

Please create a clear, well-organized summary of these notes.
Group information by the categories when possible (Places, NPCs, General Knowledge, Private Knowledge).
Highlight important details, connections, and anything that might be easy to forget.
Keep the tone useful and concise. Use bullet points and short paragraphs.
If there are no notes, just say so politely.
"""

    try:
        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",  # Good balance. Change if needed.
            max_tokens=2000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return message.content[0].text
    except Exception as e:
        return f"Error generating summary: {str(e)}"


async def run_sunday_summary():
    """Called every Sunday. Creates summary and DMs the owner."""
    try:
        owner = await bot.fetch_user(OWNER_ID)
        if not owner:
            print("Could not find owner user")
            return

        notes = await get_notes(limit=500)
        if not notes:
            await owner.send("📚 **Weekly PbP Notes Summary**\n\nYou have no notes yet. Start adding some with the right-click menu!")
            return

        # Build text for Claude
        notes_text = ""
        for n in notes:
            notes_text += f"[{n['category']}] Note #{n['id']}\n"
            notes_text += f"From: {n['author_name']}\n"
            if n['comment']:
                notes_text += f"Comment: {n['comment']}\n"
            notes_text += f"{n['content']}\n\n---\n\n"

        summary = await generate_summary_with_claude(notes_text)

        # Also create a full export
        full_export = await export_all_notes_markdown()

        # Send summary
        if len(summary) > 1900:
            # Split if too long
            parts = [summary[i:i+1900] for i in range(0, len(summary), 1900)]
            await owner.send("📚 **Weekly PbP Notes Summary (Claude)**\n")
            for i, part in enumerate(parts):
                await owner.send(f"**Part {i+1}**\n{part}")
        else:
            await owner.send(f"📚 **Weekly PbP Notes Summary (Claude)**\n\n{summary}")

        # Send full export as file
        with open("weekly_export.md", "w", encoding="utf-8") as f:
            f.write(full_export)

        await owner.send(
            content="📎 Full export of all your notes (Markdown):",
            file=discord.File("weekly_export.md")
        )

        await set_last_summary_date(datetime.now(TIMEZONE).isoformat())
        print(f"Sunday summary sent to owner at {datetime.now(TIMEZONE)}")

    except Exception as e:
        print(f"Error in Sunday summary: {e}")
        traceback.print_exc()
        try:
            owner = await bot.fetch_user(OWNER_ID)
            await owner.send(f"⚠️ There was an error generating your weekly summary:\n```{str(e)}```")
        except:
            pass


# -------------------- Checks --------------------

def is_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "This bot is private. Only the owner can use note commands.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# -------------------- Context Menu + Modal --------------------

class AddNoteModal(discord.ui.Modal, title="Add to Notes"):
    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message

        self.category = discord.ui.TextInput(
            label="Category",
            placeholder="GT / SC / GH / FE (or any custom name)",
            default="GT",
            max_length=50,
            required=True
        )
        self.comment = discord.ui.TextInput(
            label="Your comment (optional)",
            placeholder="Why is this important? Any extra notes...",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500
        )
        self.add_item(self.category)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the owner can add notes.", ephemeral=True)
            return

        cat = self.category.value.strip()
        # Soft validation – allow custom but prefer the four
        if cat not in CATEGORIES:
            # Still allow it, just warn
            pass

        content = self.message.content or "*[No text content – maybe an embed or attachment]*"
        if len(content) > 1800:
            content = content[:1800] + "..."

        note_id = await add_note(
            category=cat,
            content=content,
            comment=self.comment.value.strip() if self.comment.value else None,
            author_name=str(self.message.author),
            author_id=self.message.author.id,
            message_link=self.message.jump_url,
            channel_name=self.message.channel.name if hasattr(self.message.channel, "name") else "DM",
            added_by=interaction.user.id
        )

        await interaction.response.send_message(
            f"✅ Added to **{cat}** as Note #{note_id}",
            ephemeral=True
        )


@tree.context_menu(name="Add to Notes")
@is_owner()
async def add_to_notes_context(interaction: discord.Interaction, message: discord.Message):
    modal = AddNoteModal(message)
    await interaction.response.send_modal(modal)


# -------------------- Slash Commands --------------------

@tree.command(name="notes_list", description="List your notes (optionally by category)")
@is_owner()
@app_commands.describe(category="Filter by category")
@app_commands.choices(category=[
    app_commands.Choice(name=c, value=c) for c in CATEGORIES
])
async def notes_list(interaction: discord.Interaction, category: str = None):
    notes = await get_notes(category=category, limit=20)

    if not notes:
        await interaction.response.send_message("No notes found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Your Notes{' – ' + category if category else ''}",
        color=discord.Color.blue()
    )

    for n in notes:
        value = n["content"][:150] + ("..." if len(n["content"]) > 150 else "")
        if n["comment"]:
            value += f"\n*Comment: {n['comment'][:80]}*"
        embed.add_field(
            name=f"#{n['id']} | {n['category']}",
            value=value,
            inline=False
        )

    embed.set_footer(text="Use /notes_view <id> to see full note")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="notes_search", description="Search your notes")
@is_owner()
@app_commands.describe(query="What to search for")
async def notes_search(interaction: discord.Interaction, query: str):
    notes = await get_notes(search=query, limit=15)

    if not notes:
        await interaction.response.send_message(f"No notes found for `{query}`.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Search results for: {query}", color=discord.Color.green())
    for n in notes:
        value = n["content"][:120] + ("..." if len(n["content"]) > 120 else "")
        embed.add_field(
            name=f"#{n['id']} | {n['category']}",
            value=value,
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="notes_view", description="View a full note by ID")
@is_owner()
@app_commands.describe(note_id="The note number")
async def notes_view(interaction: discord.Interaction, note_id: int):
    note = await get_note_by_id(note_id)
    if not note:
        await interaction.response.send_message("Note not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Note #{note['id']} – {note['category']}",
        description=note["content"][:4000],
        color=discord.Color.purple()
    )
    embed.add_field(name="Author", value=note["author_name"], inline=True)
    embed.add_field(name="Channel", value=note["channel_name"], inline=True)
    embed.add_field(name="Date", value=note["created_at"][:10], inline=True)
    if note["comment"]:
        embed.add_field(name="Your comment", value=note["comment"], inline=False)
    if note["message_link"]:
        embed.add_field(name="Original message", value=f"[Jump to message]({note['message_link']})", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="notes_delete", description="Delete one of your notes")
@is_owner()
@app_commands.describe(note_id="The note number to delete")
async def notes_delete(interaction: discord.Interaction, note_id: int):
    success = await delete_note(note_id, interaction.user.id)
    if success:
        await interaction.response.send_message(f"🗑️ Note #{note_id} deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("Could not delete that note (not found or not yours).", ephemeral=True)


@tree.command(name="notes_export", description="Export all your notes as Markdown")
@is_owner()
async def notes_export(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    md = await export_all_notes_markdown()
    with open("export.md", "w", encoding="utf-8") as f:
        f.write(md)

    await interaction.followup.send(
        content="Here is your full notes export:",
        file=discord.File("export.md"),
        ephemeral=True
    )


@tree.command(name="notes_help", description="How to use the notes bot")
@is_owner()
async def notes_help(interaction: discord.Interaction):
    help_text = """
**How to use your PbP Notes Bot**

**Adding notes (two ways)**

1. **Quick reaction (easiest)**
   React to any message with:
   • 🦷 → GT
   • ☀️ → SC
   • 🏹 → GH
   • ✨ → FE

2. **Right-click menu**
   Right-click any message → Apps → **Add to Notes**
   Then choose any category and (optionally) add a comment.

**Commands**
• `/notes_list` – Show recent notes
• `/notes_search <query>` – Search your notes
• `/notes_view <id>` – See the full note
• `/notes_delete <id>` – Delete a note
• `/notes_export` – Download everything as Markdown
• `/notes_help` – This message

**Automatic Sunday Summary**
Every Sunday the bot will:
1. Collect all your notes
2. Ask Claude to create a clear summary
3. Send the summary + full export to you in a private DM
"""
    await interaction.response.send_message(help_text, ephemeral=True)


# -------------------- Bot Events --------------------

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Quick-add notes by reacting with specific emojis."""
    # Only the owner can use this
    if payload.user_id != OWNER_ID:
        return

    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)
    category = REACTION_MAP.get(emoji)
    if not category:
        return

    # Fetch the message
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception:
        return

    content = message.content or "*[No text content – maybe an embed or attachment]*"
    if len(content) > 1800:
        content = content[:1800] + "..."

    note_id = await add_note(
        category=category,
        content=content,
        comment=None,
        author_name=str(message.author),
        author_id=message.author.id,
        message_link=message.jump_url,
        channel_name=getattr(message.channel, "name", "DM"),
        added_by=payload.user_id
    )

    # Send a quiet confirmation to the owner via DM
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(
            f"✅ Quick-added to **{category}** as Note #{note_id}\n"
            f"From: {message.author} in #{getattr(message.channel, 'name', 'DM')}\n"
            f"[Jump to message]({message.jump_url})"
        )
    except Exception:
        pass  # If DMs are closed, just stay silent


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    await init_db()

    # Sync commands
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Schedule the Sunday job (every Sunday at 18:00 local time)
    if not scheduler.running:
        scheduler.add_job(
            run_sunday_summary,
            CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE),
            id="sunday_summary",
            replace_existing=True
        )
        scheduler.start()
        print("Scheduler started – Sunday summaries at 18:00")


@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in {event}")
    traceback.print_exc()


# -------------------- Run --------------------

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN is missing in .env")
    elif not OWNER_ID:
        print("ERROR: OWNER_ID is missing in .env")
    else:
        bot.run(TOKEN)
