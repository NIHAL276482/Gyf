import asyncio
import logging
import re
import json
import sqlite3
import hashlib
import random
import string
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Union
from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, 
    ChatMemberAdministrator, ChatMemberOwner
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_name: str = 'group_manager.db'):
        self.conn = sqlite3.connect(db_name)
        self.conn.execute("PRAGMA foreign_keys = 1;")
        self.create_tables()
    
    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.executescript('''
            CREATE TABLE IF NOT EXISTS groups (
                group_id INTEGER PRIMARY KEY,
                title TEXT,
                settings TEXT,
                created_at TIMESTAMP,
                welcome_message TEXT,
                rules TEXT
            );
            
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                group_id INTEGER,
                username TEXT,
                role TEXT DEFAULT 'member',
                joined_at TIMESTAMP,
                warnings INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES groups (group_id),
                PRIMARY KEY (user_id, group_id)
            );
            
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                group_id INTEGER,
                user_id INTEGER,
                content TEXT,
                timestamp TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES groups (group_id),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            );
            
            CREATE TABLE IF NOT EXISTS shortened_urls (
                short_code TEXT PRIMARY KEY,
                original_url TEXT,
                created_at TIMESTAMP,
                created_by INTEGER
            );
            
            CREATE TABLE IF NOT EXISTS polls (
                poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                question TEXT,
                options TEXT,
                created_at TIMESTAMP,
                is_active INTEGER DEFAULT 1
            );
            
            CREATE TABLE IF NOT EXISTS poll_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                poll_id INTEGER,
                user_id INTEGER,
                selected_option TEXT,
                FOREIGN KEY (poll_id) REFERENCES polls (poll_id)
            );
            
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER,
                title TEXT,
                scheduled_time TIMESTAMP,
                description TEXT,
                created_by INTEGER
            );
            
            CREATE TABLE IF NOT EXISTS event_rsvps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                user_id INTEGER,
                status TEXT,
                FOREIGN KEY (event_id) REFERENCES events (event_id)
            );
        ''')
        self.conn.commit()

# Utility function to get role priority
# Higher return value indicates higher privilege
# E.g: owner -> 4, admin -> 3, moderator -> 2, member -> 1
def get_role_priority(role: str) -> int:
    role_map = {
        'owner': 4,
        'creator': 4,
        'superadmin': 3,
        'administrator': 3,
        'admin': 3,
        'moderator': 2,
        'member': 1,
        'restricted': 0
    }
    return role_map.get(role.lower(), 1)

class GroupManager:
    def __init__(self, token: str):
        self.db = Database()
        self.application = Application.builder().token(token).build()
        self.setup_handlers()

    def setup_handlers(self):
        command_handlers = [
            CommandHandler("start", self.cmd_start),
            CommandHandler("help", self.cmd_help),
            CommandHandler("rules", self.cmd_rules),
            CommandHandler("setrules", self.cmd_setrules),
            CommandHandler("ban", self.cmd_ban),
            CommandHandler("unban", self.cmd_unban),
            CommandHandler("mute", self.cmd_mute),
            CommandHandler("unmute", self.cmd_unmute),
            CommandHandler("warn", self.cmd_warn),
            CommandHandler("shorturl", self.cmd_shorturl),
            CommandHandler("setwelcome", self.cmd_setwelcome),
            CommandHandler("points", self.cmd_points),
            CommandHandler("poll", self.cmd_poll),
            CommandHandler("vote", self.cmd_vote),
            CommandHandler("stoppoll", self.cmd_stoppoll),
            CommandHandler("createevent", self.cmd_createevent),
            CommandHandler("rsvp", self.cmd_rsvp),
            CommandHandler("showevents", self.cmd_showevents),
            CommandHandler("promote", self.cmd_promote),
            CommandHandler("demote", self.cmd_demote),
            CommandHandler("lockdown", self.cmd_lockdown)
        ]
        
        message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        callback_handler = CallbackQueryHandler(self.handle_callback)

        for handler in command_handlers:
            self.application.add_handler(handler)

        self.application.add_handler(message_handler)
        self.application.add_handler(callback_handler)

    async def check_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Check if user is at least an admin in the chat."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        
        try:
            member_info = await context.bot.get_chat_member(chat_id, user_id)
            status = member_info.status
            # Map Telegram statuses to something akin to role priorities
            if status in ['administrator', 'creator']:
                return True
            return False
        except Exception as e:
            logger.error(f"Admin check error: {e}")
            return False

    async def get_user_role(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Attempt to retrieve the user's stored role from the DB if present; fallback to Telegram chat status."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT role FROM users WHERE user_id=? AND group_id=?", (user_id, chat_id))
        row = cursor.fetchone()

        if row:
            return row[0]
        else:
            # Fallback to actual Telegram role
            try:
                member_info = await context.bot.get_chat_member(chat_id, user_id)
                return member_info.status
            except Exception:
                return "member"

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initial welcome message and registration."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        
        # Make sure a record for the group and the user is in the DB
        cursor = self.db.conn.cursor()
        
        # Insert group if not present
        cursor.execute("INSERT OR IGNORE INTO groups (group_id, title, created_at) VALUES (?, ?, ?)",
                       (chat_id, update.effective_chat.title or "Unnamed Group", datetime.now()))
        
        # Insert user if not present
        cursor.execute("""INSERT OR IGNORE INTO users (user_id, group_id, username, joined_at)
                          VALUES (?, ?, ?, ?)""",
                       (user_id, chat_id, username, datetime.now()))
        self.db.conn.commit()

        welcome_text = (
            "Hello! I'm your expanded GroupManager Bot.\n"
            "• Use /help to see what I can do.\n"
            "• Use /setwelcome <message> to customize an automatic welcome.\n"
            "• Use /setrules <rules> to set or update the group rules.\n\n"
            "Happy managing!"
        )
        await update.message.reply_text(welcome_text)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display help text for all commands."""
        help_text = (
            "**Available Commands:**\n\n"
            "/start - Bot introduction and setup\n"
            "/help - Display this help message\n"
            "/rules - Show current group rules\n"
            "/setrules <rules> - Set or update group rules (admin only)\n"
            "/ban <user_id> [reason] - Ban a user (admin only)\n"
            "/unban <user_id> - Unban a user (admin only)\n"
            "/mute <user_id> [minutes] - Temporarily mute a user (admin only)\n"
            "/unmute <user_id> - Unmute a user (admin only)\n"
            "/warn <user_id> [reason] - Warn a user (admin only)\n"
            "/shorturl <url> - Shorten a given URL\n"
            "/setwelcome <message> - Customize welcome message (admin only)\n"
            "/points <user_id> - Check a user’s points\n"
            "/poll <question>|<option1>|<option2>|... - Create a poll\n"
            "/vote <poll_id> <option> - Vote on a poll\n"
            "/stoppoll <poll_id> - Stop an active poll (admin only)\n"
            "/createevent <title>|<YYYY-MM-DD HH:MM>|<description> - Schedule an event\n"
            "/rsvp <event_id> <yes/no/maybe> - RSVP to an event\n"
            "/showevents - Show upcoming events\n"
            "/promote <user_id> <role> - Promote a user to a higher role (owner/admin only)\n"
            "/demote <user_id> - Demote a user to a lower role (owner/admin only)\n"
            "/lockdown <on/off> - Restrict or allow messages for most users\n"
        )
        await update.message.reply_markdown(help_text)

    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current group rules."""
        chat_id = update.effective_chat.id
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT rules FROM groups WHERE group_id=?", (chat_id,))
        row = cursor.fetchone()

        if row and row[0]:
            await update.message.reply_text(f"**Group Rules:**\n{row[0]}")
        else:
            await update.message.reply_text("No rules set. Use /setrules <rules> to add some.")

    async def cmd_setrules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set or update the group rules (admin only)."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ You must be an admin to use this command.")
            return
        
        if not context.args:
            await update.message.reply_text("Usage: /setrules <rules text>")
            return
        
        new_rules = " ".join(context.args)
        chat_id = update.effective_chat.id

        cursor = self.db.conn.cursor()
        cursor.execute("UPDATE groups SET rules=? WHERE group_id=?", (new_rules, chat_id))
        self.db.conn.commit()
        
        await update.message.reply_text("✅ Rules updated successfully.")

    async def cmd_ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ban a user from the group."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        try:
            if not context.args:
                await update.message.reply_text("Usage: /ban <user_id> [reason]")
                return

            user_id = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason given"
            
            await context.bot.ban_chat_member(
                chat_id=update.effective_chat.id,
                user_id=user_id
            )
            await update.message.reply_text(f"User {user_id} banned. Reason: {reason}")
        except Exception as e:
            await update.message.reply_text(f"Failed to ban user: {str(e)}")
            logger.error(f"Ban error: {e}")

    async def cmd_unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unban a user from the group."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /unban <user_id>")
            return

        user_id = int(context.args[0])
        try:
            await context.bot.unban_chat_member(
                chat_id=update.effective_chat.id,
                user_id=user_id
            )
            await update.message.reply_text(f"User {user_id} has been unbanned.")
        except Exception as e:
            await update.message.reply_text(f"Error unbanning user: {str(e)}")
            logger.error(f"Unban error: {e}")

    async def cmd_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Mute a user for X minutes."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return
        
        if not context.args:
            await update.message.reply_text("Usage: /mute <user_id> [minutes]")
            return

        user_id = int(context.args[0])
        mute_minutes = int(context.args[1]) if len(context.args) > 1 else 10  # default 10 minutes
        until_date = datetime.now() + timedelta(minutes=mute_minutes)

        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await update.message.reply_text(f"Muted user {user_id} for {mute_minutes} minutes.")
        except Exception as e:
            await update.message.reply_text(f"Failed to mute user: {str(e)}")
            logger.error(f"Mute error: {e}")

    async def cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unmute a user."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /unmute <user_id>")
            return

        user_id = int(context.args[0])
        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=user_id,
                permissions=ChatPermissions(can_send_messages=True,
                                            can_send_media_messages=True,
                                            can_send_polls=True,
                                            can_send_other_messages=True,
                                            can_add_web_page_previews=True,
                                            can_change_info=False,
                                            can_invite_users=True,
                                            can_pin_messages=False)
            )
            await update.message.reply_text(f"User {user_id} has been unmuted.")
        except Exception as e:
            await update.message.reply_text(f"Failed to unmute user: {str(e)}")
            logger.error(f"Unmute error: {e}")

    async def cmd_warn(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Warn a user."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /warn <user_id> [reason]")
            return

        user_id = int(context.args[0])
        reason = " ".join(context.args[1:]) if len(context.args) > 1 else "No reason provided"

        chat_id = update.effective_chat.id
        cursor = self.db.conn.cursor()
        cursor.execute("""
            UPDATE users
            SET warnings = warnings + 1
            WHERE user_id = ? AND group_id = ?
        """, (user_id, chat_id))
        self.db.conn.commit()

        # Get updated warnings count
        cursor.execute("""
            SELECT warnings FROM users WHERE user_id=? AND group_id=?
        """, (user_id, chat_id))
        row = cursor.fetchone()
        if row:
            warning_count = row[0]
            await update.message.reply_text(
                f"User {user_id} warned. Reason: {reason}\nTotal warnings: {warning_count}"
            )
            # Optional auto-kick if warnings exceed threshold
            if warning_count >= 3:
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                    await update.message.reply_text(
                        f"User {user_id} has been auto-banned for exceeding warning limit."
                    )
                except Exception as e:
                    logger.error(f"Auto-ban after warnings failed: {e}")
        else:
            await update.message.reply_text("Cannot warn user who is not in the database.")

    async def cmd_shorturl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shorten a given URL."""
        if not context.args:
            await update.message.reply_text("Usage: /shorturl <url>")
            return
            
        url = context.args[0]
        if not re.match(r'https?://', url):
            url = 'http://' + url

        short_url = await self.shorten_url(url, update.effective_user.id)
        await update.message.reply_text(f"Shortened URL: {short_url}")

    async def shorten_url(self, original_url: str, created_by: int) -> str:
        """Generate and store a short code for a given URL."""
        short_code = ''.join(random.choices(string.ascii_letters + string.digits, k=6))
        short_link = f"https://t.ly/{short_code}"

        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT INTO shortened_urls (short_code, original_url, created_at, created_by) 
            VALUES (?, ?, ?, ?)
        """, (short_code, original_url, datetime.now(), created_by))
        self.db.conn.commit()

        return short_link

    async def cmd_setwelcome(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set the group's custom welcome message."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return
        
        if not context.args:
            await update.message.reply_text("Usage: /setwelcome <message>")
            return
        
        chat_id = update.effective_chat.id
        new_welcome = " ".join(context.args)
        cursor = self.db.conn.cursor()
        cursor.execute("UPDATE groups SET welcome_message=? WHERE group_id=?", (new_welcome, chat_id))
        self.db.conn.commit()

        await update.message.reply_text("✅ Welcome message updated.")

    async def cmd_points(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Check a user’s points or your own if no argument is given."""
        chat_id = update.effective_chat.id
        if context.args:
            user_id = int(context.args[0])
        else:
            user_id = update.effective_user.id
        
        cursor = self.db.conn.cursor()
        cursor.execute(
            "SELECT points FROM users WHERE user_id=? AND group_id=?",
            (user_id, chat_id)
        )
        row = cursor.fetchone()
        if row:
            await update.message.reply_text(f"User {user_id} has {row[0]} points.")
        else:
            await update.message.reply_text("User not found in the database.")

    async def cmd_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create a poll with multiple options, usage: /poll question|option1|option2|..."""
        if len(context.args) == 0:
            await update.message.reply_text("Usage: /poll question|option1|option2|...")
            return
        text = " ".join(context.args)
        parts = text.split("|")
        
        if len(parts) < 2:
            await update.message.reply_text("Provide a question and at least one option.")
            return

        question = parts[0]
        options = parts[1:]
        chat_id = update.effective_chat.id
        
        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT INTO polls (group_id, question, options, created_at)
            VALUES (?, ?, ?, ?)
        """, (chat_id, question, json.dumps(options), datetime.now()))
        poll_id = cursor.lastrowid
        self.db.conn.commit()

        await update.message.reply_text(
            f"Poll created (ID: {poll_id}): {question}\nOptions:\n" +
            "\n".join([f"- {opt}" for opt in options]) +
            "\nUse /vote <poll_id> <option> to vote."
        )

    async def cmd_vote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Vote on a poll: /vote <poll_id> <option>"""
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /vote <poll_id> <option>")
            return
        
        poll_id = int(context.args[0])
        selected_option = " ".join(context.args[1:])
        user_id = update.effective_user.id
        
        # Check if poll is active
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT options, is_active FROM polls WHERE poll_id=?", (poll_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("No such poll found.")
            return
        options, is_active = row
        if not is_active:
            await update.message.reply_text("Poll is not active.")
            return

        options_list = json.loads(options)
        if selected_option not in options_list:
            await update.message.reply_text("Invalid option. Check poll options.")
            return

        # Record the vote
        cursor.execute("""
            INSERT INTO poll_responses (poll_id, user_id, selected_option)
            VALUES (?, ?, ?)
        """, (poll_id, user_id, selected_option))
        self.db.conn.commit()

        await update.message.reply_text(f"Vote recorded for poll {poll_id}.")

    async def cmd_stoppoll(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop a poll and show final results."""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        if not context.args:
            await update.message.reply_text("Usage: /stoppoll <poll_id>")
            return

        poll_id = int(context.args[0])
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT options FROM polls WHERE poll_id=?", (poll_id,))
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text("No poll found with that ID.")
            return
        
        # Mark poll as inactive
        cursor.execute("UPDATE polls SET is_active=0 WHERE poll_id=?", (poll_id,))
        self.db.conn.commit()

        options_list = json.loads(row[0])
        # Tally votes
        results = {opt: 0 for opt in options_list}
        cursor.execute("SELECT selected_option FROM poll_responses WHERE poll_id=?", (poll_id,))
        votes = cursor.fetchall()
        for (selected,) in votes:
            if selected in results:
                results[selected] += 1
        
        result_text = f"**Poll {poll_id} Results:**\n"
        for opt in results:
            result_text += f"{opt}: {results[opt]} votes\n"
        await update.message.reply_markdown(result_text)

    async def cmd_createevent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create an event: /createevent <title>|<YYYY-MM-DD HH:MM>|<description>"""
        if len(context.args) == 0:
            await update.message.reply_text("Usage: /createevent <title>|<YYYY-MM-DD HH:MM>|<description>")
            return
        raw_text = " ".join(context.args)
        parts = raw_text.split("|")
        if len(parts) < 3:
            await update.message.reply_text("Provide a title, a datetime, and a description.")
            return
        
        title = parts[0].strip()
        date_str = parts[1].strip()
        description = parts[2].strip()
        
        try:
            scheduled_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except ValueError:
            await update.message.reply_text("Datetime format should be YYYY-MM-DD HH:MM.")
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT INTO events (group_id, title, scheduled_time, description, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, title, scheduled_time, description, user_id))
        self.db.conn.commit()
        
        await update.message.reply_text(f"Event '{title}' created for {scheduled_time}.")

    async def cmd_rsvp(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """RSVP to an event: /rsvp <event_id> <yes/no/maybe>"""
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /rsvp <event_id> <yes/no/maybe>")
            return
        
        event_id = int(context.args[0])
        status = context.args[1].lower()
        if status not in ['yes', 'no', 'maybe']:
            await update.message.reply_text("Invalid response. Use yes, no, or maybe.")
            return
        
        user_id = update.effective_user.id
        cursor = self.db.conn.cursor()
        
        # Check if event exists
        cursor.execute("SELECT title FROM events WHERE event_id=?", (event_id,))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("No such event found.")
            return
        event_title = row[0]
        
        # Insert or update RSVP
        cursor.execute("""
            INSERT INTO event_rsvps (event_id, user_id, status)
            VALUES (?, ?, ?)
            ON CONFLICT(event_id, user_id) DO UPDATE SET status=excluded.status
        """, (event_id, user_id, status))
        self.db.conn.commit()

        await update.message.reply_text(f"RSVP recorded for event '{event_title}'. You answered '{status}'.")

    async def cmd_showevents(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show upcoming events."""
        chat_id = update.effective_chat.id
        cursor = self.db.conn.cursor()
        now = datetime.now()
        cursor.execute("""
            SELECT event_id, title, scheduled_time 
            FROM events
            WHERE group_id=? AND scheduled_time >= ?
            ORDER BY scheduled_time ASC
        """, (chat_id, now))

        rows = cursor.fetchall()
        if not rows:
            await update.message.reply_text("No upcoming events.")
            return
        
        msg = "**Upcoming Events:**\n"
        for (eid, title, stime) in rows:
            msg += f"Event {eid}: {title} at {stime}\n"
        await update.message.reply_markdown(msg)

    async def cmd_promote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Promote a user to a higher role: /promote <user_id> <role>"""
        # Must be group owner or top-level admin
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /promote <user_id> <role>")
            return
        
        user_id = int(context.args[0])
        new_role = context.args[1].lower()
        valid_roles = ['owner','administrator','admin','moderator','member']
        if new_role not in valid_roles:
            await update.message.reply_text("Invalid role. Valid roles: owner, administrator, moderator, member.")
            return
        
        chat_id = update.effective_chat.id
        # Retrieve current user's role
        current_user_role = await self.get_user_role(update, context)
        # Ensure user cannot promote to or above own level
        if get_role_priority(new_role) >= get_role_priority(current_user_role):
            await update.message.reply_text("Cannot promote to a role >= your own role.")
            return

        cursor = self.db.conn.cursor()
        cursor.execute("""
            UPDATE users SET role=? WHERE user_id=? AND group_id=?
        """, (new_role, user_id, chat_id))
        self.db.conn.commit()
        
        await update.message.reply_text(f"User {user_id} promoted to {new_role}.")

    async def cmd_demote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Demote a user to a lower role: /demote <user_id>"""
        # Must be group owner or top-level admin
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return

        if len(context.args) < 1:
            await update.message.reply_text("Usage: /demote <user_id>")
            return
        
        user_id = int(context.args[0])
        chat_id = update.effective_chat.id
        
        # Retrieve current user's role
        current_user_role = await self.get_user_role(update, context)

        # Retrieve target user's role
        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT role FROM users WHERE user_id=? AND group_id=?
        """, (user_id, chat_id))
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("User not found in the database.")
            return
        target_role = row[0]

        # Must have higher role to demote
        if get_role_priority(target_role) >= get_role_priority(current_user_role):
            await update.message.reply_text("You cannot demote a user with a role >= your own.")
            return

        # Downgrade logic: owner -> admin, admin -> moderator, moderator -> member, etc.
        old_priority = get_role_priority(target_role)
        new_priority = old_priority - 1
        # Determine new role name from new_priority
        for r, p in [('owner',4), ('administrator',3), ('moderator',2), ('member',1)]:
            if p == new_priority:
                new_role = r
                break
        else:
            new_role = 'member'

        cursor.execute("""
            UPDATE users SET role=? WHERE user_id=? AND group_id=?
        """, (new_role, user_id, chat_id))
        self.db.conn.commit()

        await update.message.reply_text(f"User {user_id} has been demoted to {new_role}.")

    async def cmd_lockdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Restrict or allow messages for non-admins. Usage: /lockdown <on/off>"""
        if not await self.check_admin(update, context):
            await update.message.reply_text("❌ Admin privileges required.")
            return
        
        if not context.args:
            await update.message.reply_text("Usage: /lockdown <on/off>")
            return
        
        mode = context.args[0].lower()
        chat_id = update.effective_chat.id
        try:
            if mode == "on":
                # Restrict messages for members
                await context.bot.set_chat_permissions(
                    chat_id=chat_id,
                    permissions=ChatPermissions(
                        can_send_messages=False,
                        can_send_media_messages=False,
                        can_send_polls=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False
                    )
                )
                await update.message.reply_text("Lockdown mode enabled. Non-admins cannot send messages.")
            elif mode == "off":
                # Restore normal permission
                await context.bot.set_chat_permissions(
                    chat_id=chat_id,
                    permissions=ChatPermissions(
                        can_send_messages=True,
                        can_send_media_messages=True,
                        can_send_polls=True,
                        can_send_other_messages=True,
                        can_add_web_page_previews=True
                    )
                )
                await update.message.reply_text("Lockdown mode disabled. Everyone can send messages now.")
            else:
                await update.message.reply_text("Use /lockdown <on/off>")
        except Exception as e:
            logger.error(f"Lockdown error: {e}")
            await update.message.reply_text("Failed to change lockdown mode.")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all text messages (non-command). For advanced spam checks, awarding points, etc."""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        username = update.effective_user.username or "NoUsername"
        message_text = update.message.text

        # Ensure user is in DB
        cursor = self.db.conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, group_id, username, joined_at) VALUES (?, ?, ?, ?)",
                       (user_id, chat_id, username, datetime.now()))
        self.db.conn.commit()

        # Simple point awarding system
        cursor.execute("UPDATE users SET points = points + 1 WHERE user_id=? AND group_id=?", (user_id, chat_id))
        self.db.conn.commit()

        # Save message to DB (for logging, analytics, etc.)
        cursor.execute("""
            INSERT INTO messages (group_id, user_id, content, timestamp)
            VALUES (?, ?, ?, ?)
        """, (chat_id, user_id, message_text, datetime.now()))
        self.db.conn.commit()

        # Optionally, check for spam/triggers here (placeholder)
        # ...

        # Optionally, welcome new members if joined recently and a welcome_message is set
        # ...

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Handle button presses and inline keyboard callbacks.
        This example is a placeholder to show how you'd handle them.
        """
        query = update.callback_query
        await query.answer()

        # Perform logic based on query.data
        if query.data.startswith("some_action"):
            await query.edit_message_text(text="Action handled!")
        else:
            await query.edit_message_text(text="Unknown action.")

    def run(self):
        """Start the bot with long-polling."""
        self.application.run_polling()

if __name__ == '__main__':
    bot = GroupManager("8131621643:AAFKkU4pWnDhLcYXTjMsTbeY11gQalaUSNs")
    bot.run()
