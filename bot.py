"""
JM2E Telegram Bot: Convert JMComic IDs to E-Hentai/ExHentai links.
"""

import logging
import os
import re
from typing import Optional
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from jm2e import JM2EConverter, ConversionResult

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token (from environment variable)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Global converter cache (keyed by cookie hash for reuse)
_converters: dict[str, JM2EConverter] = {}


def get_converter(exhentai_cookie: Optional[str] = None) -> JM2EConverter:
    """Get or create a converter instance.

    Args:
        exhentai_cookie: Optional ExHentai cookie for accessing exhentai.org

    Returns:
        JM2EConverter instance (cached by cookie hash)
    """
    cache_key = str(hash(exhentai_cookie)) if exhentai_cookie else "default"

    if cache_key not in _converters:
        _converters[cache_key] = JM2EConverter(exhentai_cookie=exhentai_cookie)

    return _converters[cache_key]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    # Check if user has ExHentai cookie set
    has_cookie = context.user_data.get("exhentai_cookie") is not None
    cookie_status = "✅ ExHentai cookie set" if has_cookie else "❌ No ExHentai cookie"

    await update.message.reply_text(
        "🔗 *JM2E Bot* - JMComic to E-Hentai/ExHentai Converter\n\n"
        "Send me a JMComic ID and I'll find the link for you!\n\n"
        f"*Status:* {cookie_status}\n\n"
        "*Example:* `1180203` or `/jm 1180203`\n\n"
        "*Search priority:*\n"
        "1. ExHentai (if cookie set)\n"
        "2. E-Hentai\n"
        "3. wnacg\n\n"
        "Use `/setcookie` to enable ExHentai search.",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "📖 *How to use JM2E Bot*\n\n"
        "*Basic usage:*\n"
        "• Send a JMComic ID directly: `1180203`\n"
        "• Use command: `/jm 1180203`\n"
        "• Multiple IDs: `/jm 1180203 540930`\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/help - Show this help\n"
        "/jm <id> - Convert JMComic ID to link\n"
        "/setcookie <cookie> - Set ExHentai cookie\n"
        "/clearcookie - Remove ExHentai cookie\n"
        "/status - Check current settings\n\n"
        "*ExHentai Cookie:*\n"
        "To access ExHentai, set your cookie with:\n"
        "`/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx; igneous=xxx`\n\n"
        "Get your cookie from browser DevTools after logging into exhentai.org",
        parse_mode="Markdown",
    )


async def set_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set ExHentai cookie for the user."""
    if not context.args:
        await update.message.reply_text(
            "🍪 *Set ExHentai Cookie*\n\n"
            "Usage: `/setcookie <cookie_string>`\n\n"
            "*Supported formats:*\n"
            "1. Standard cookie format:\n"
            "`/setcookie ipb_member_id=123; ipb_pass_hash=abc; igneous=xyz`\n\n"
            "2. Key: value format (from DevTools):\n"
            "`/setcookie ipb_member_id: 123`\n"
            "`ipb_pass_hash: abc`\n"
            "`igneous: xyz`\n\n"
            "*How to get your cookie:*\n"
            "1. Log in to exhentai.org in your browser\n"
            "2. Open DevTools (F12) → Application → Cookies\n"
            "3. Copy `ipb_member_id`, `ipb_pass_hash`, and `igneous`",
            parse_mode="Markdown",
        )
        return

    # Join all args and handle multi-line input
    raw_input = " ".join(context.args)

    # Also check if there's multi-line text in the message
    message_text = update.message.text
    if message_text.startswith("/setcookie"):
        raw_input = message_text[len("/setcookie") :].strip()

    # Normalize the cookie format
    cookie = _normalize_cookie(raw_input)

    if not cookie:
        await update.message.reply_text(
            "❌ Could not parse cookie.\n\n"
            "Please provide cookie in one of these formats:\n"
            "• `ipb_member_id=xxx; ipb_pass_hash=xxx; igneous=xxx`\n"
            "• `ipb_member_id: xxx` (one per line)",
            parse_mode="Markdown",
        )
        return

    # Basic validation: check for required cookie fields
    required_fields = ["ipb_member_id", "ipb_pass_hash"]
    missing = [f for f in required_fields if f not in cookie]

    if missing:
        await update.message.reply_text(
            f"❌ Invalid cookie format.\n\n"
            f"Missing required fields: `{', '.join(missing)}`\n\n"
            "Cookie must contain at least:\n"
            "• `ipb_member_id=...`\n"
            "• `ipb_pass_hash=...`",
            parse_mode="Markdown",
        )
        return

    # Store cookie in user data
    context.user_data["exhentai_cookie"] = cookie

    # Delete the user's message containing the cookie (security)
    try:
        await update.message.delete()
        deleted = True
    except Exception:
        deleted = False

    # Send confirmation to the chat (not as reply since message may be deleted)
    confirm_text = (
        "✅ ExHentai cookie saved!\n\nYour searches will now use ExHentai first."
    )
    if deleted:
        confirm_text += "\nThe cookie message has been deleted for security."

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=confirm_text,
        parse_mode="Markdown",
    )


def _normalize_cookie(raw: str) -> Optional[str]:
    """Normalize cookie input to standard format.

    Accepts:
    - Standard: "ipb_member_id=123; ipb_pass_hash=abc; igneous=xyz"
    - Key: value: "ipb_member_id: 123\\nipb_pass_hash: abc"
    - Mixed formats

    Returns:
    - Normalized cookie string: "ipb_member_id=123; ipb_pass_hash=abc; igneous=xyz"
    - None if parsing failed
    """
    if not raw:
        return None

    parts = {}

    # Split by actual newlines and semicolons
    # Note: Use explicit newline character, not \n in character class
    lines = raw.replace(";", "\n").split("\n")

    for token in lines:
        token = token.strip()
        if not token:
            continue

        # Try "key: value" format (from DevTools)
        if ": " in token:
            key, _, value = token.partition(": ")
            key = key.strip()
            value = value.strip()
            if key and value:
                parts[key] = value
                continue

        # Try "key=value" format (standard cookie)
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value:
                parts[key] = value
                continue

    if not parts:
        return None

    # Build normalized cookie string
    return "; ".join(f"{k}={v}" for k, v in parts.items())


async def clear_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear ExHentai cookie for the user."""
    if "exhentai_cookie" in context.user_data:
        del context.user_data["exhentai_cookie"]
        await update.message.reply_text(
            "🗑️ ExHentai cookie cleared.\n\nSearches will now use E-Hentai only.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "ℹ️ No ExHentai cookie was set.",
            parse_mode="Markdown",
        )


async def toggle_wnacg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle WNACG-only mode (disable E-Hentai search)."""
    current = context.user_data.get("wnacg_only", False)
    context.user_data["wnacg_only"] = not current

    if not current:
        await update.message.reply_text(
            "📗 *WNACG-only mode enabled*\n\nE-Hentai search is now disabled.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            "🔄 *WNACG-only mode disabled*\n\nE-Hentai search is now enabled.",
            parse_mode="Markdown",
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current user settings."""
    has_cookie = context.user_data.get("exhentai_cookie") is not None
    wnacg_only = context.user_data.get("wnacg_only", False)

    if wnacg_only:
        priority = "wnacg only"
    elif has_cookie:
        priority = "ExHentai → E-Hentai → wnacg"
    else:
        priority = "E-Hentai → wnacg"

    status_text = (
        "📊 *Current Settings*\n\n"
        f"ExHentai cookie: {'✅ Set' if has_cookie else '❌ Not set'}\n"
        f"WNACG-only mode: {'✅ On' if wnacg_only else '❌ Off'}\n"
        f"Search priority: {priority}"
    )

    await update.message.reply_text(status_text, parse_mode="Markdown")


async def convert_jm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert JMComic ID from command arguments."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a JMComic ID. Example: `/jm 1180203`", parse_mode="Markdown"
        )
        return

    for jm_id in context.args:
        await process_jm_id(update, context, jm_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages containing JMComic IDs."""
    text = update.message.text.strip()

    # Extract all numbers that look like JMComic IDs (5-7 digits)
    jm_ids = re.findall(r"\b(\d{5,7})\b", text)

    if not jm_ids:
        return  # Silently ignore non-ID messages

    for jm_id in jm_ids:
        await process_jm_id(update, context, jm_id)


async def process_jm_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE, jm_id: str
) -> None:
    """Process a single JMComic ID and send the result."""
    # Validate ID format
    if not jm_id.isdigit():
        await update.message.reply_text(
            f"❌ Invalid ID: `{jm_id}` (must be a number)", parse_mode="Markdown"
        )
        return

    # Get user's ExHentai cookie if set
    exhentai_cookie = context.user_data.get("exhentai_cookie")
    wnacg_only = context.user_data.get("wnacg_only", False)

    # Send "processing" message
    processing_msg = await update.message.reply_text(f"🔍 Looking up JM{jm_id}...")

    try:
        conv = get_converter(exhentai_cookie)
        result = conv.convert(jm_id, wnacg_only=wnacg_only)

        # Format response based on source
        source_emoji = {
            "exhentai": "🔞",
            "ehentai": "✅",
            "wnacg": "📗",
            "hitomi": "🔶",
            "google": "🔍",
            "none": "❌",
        }

        source_name = {
            "exhentai": "ExHentai",
            "ehentai": "E-Hentai",
            "wnacg": "绅士漫画",
            "hitomi": "Hitomi.la (search)",
            "google": "Google (search)",
            "none": "Not found",
        }

        emoji = source_emoji.get(result.source, "📎")
        source = source_name.get(result.source, result.source)

        if result.source == "none" or not result.link:
            response = (
                f"❌ *JM{jm_id}*\n\n"
                f"📚 *Title:* {result.title}\n"
                f"✍️ *Author:* {result.author}\n\n"
                "No matching gallery found."
            )
        else:
            response = (
                f"{emoji} *JM{jm_id}*\n\n"
                f"📚 *Title:* {result.title}\n"
                f"✍️ *Author:* {result.author}\n"
                f"🔗 *Source:* {source}\n\n"
                f"[Open Link]({result.link})"
            )

        await processing_msg.edit_text(
            response, parse_mode="Markdown", disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Error processing JM{jm_id}: {e}")
        await processing_msg.edit_text(f"❌ Error processing JM{jm_id}: {str(e)}")


def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set")

    # Create the Application with persistence for user data
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("jm", convert_jm))
    application.add_handler(CommandHandler("setcookie", set_cookie))
    application.add_handler(CommandHandler("clearcookie", clear_cookie))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("wnacg", toggle_wnacg))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Run the bot
    logger.info("Starting JM2E Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
