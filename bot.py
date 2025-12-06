"""
JM2E Telegram Bot: Convert JMComic IDs to E-Hentai links.
"""

import logging
import re
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

# Telegram Bot Token
BOT_TOKEN = "8307297002:AAHmvY2Ho8mL3oFNmX_YTLH2c813gPYKKm0"

# Global converter instance
converter: JM2EConverter = None


def get_converter() -> JM2EConverter:
    """Get or create the converter instance."""
    global converter
    if converter is None:
        converter = JM2EConverter()
    return converter


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text(
        "🔗 JM2E Bot - JMComic to E-Hentai Converter\n\n"
        "Send me a JMComic ID (number) and I'll find the E-Hentai link for you!\n\n"
        "Example: `1180203` or `/jm 1180203`\n\n"
        "Fallback chain:\n"
        "1. E-Hentai direct link\n"
        "2. Hitomi.la search\n"
        "3. Google search",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text(
        "📖 *How to use JM2E Bot*\n\n"
        "1. Send a JMComic ID directly: `1180203`\n"
        "2. Use command: `/jm 1180203`\n"
        "3. Multiple IDs: `/jm 1180203 540930 1192427`\n\n"
        "*Commands:*\n"
        "/start - Start the bot\n"
        "/help - Show this help\n"
        "/jm <id> - Convert JMComic ID to link",
        parse_mode="Markdown",
    )


async def convert_jm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert JMComic ID from command arguments."""
    if not context.args:
        await update.message.reply_text(
            "Please provide a JMComic ID. Example: `/jm 1180203`", parse_mode="Markdown"
        )
        return

    for jm_id in context.args:
        await process_jm_id(update, jm_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages containing JMComic IDs."""
    text = update.message.text.strip()

    # Extract all numbers that look like JMComic IDs (5-7 digits)
    jm_ids = re.findall(r"\b(\d{5,7})\b", text)

    if not jm_ids:
        return  # Silently ignore non-ID messages

    for jm_id in jm_ids:
        await process_jm_id(update, jm_id)


async def process_jm_id(update: Update, jm_id: str) -> None:
    """Process a single JMComic ID and send the result."""
    # Validate ID format
    if not jm_id.isdigit():
        await update.message.reply_text(
            f"❌ Invalid ID: `{jm_id}` (must be a number)", parse_mode="Markdown"
        )
        return

    # Send "processing" message
    processing_msg = await update.message.reply_text(f"🔍 Looking up JM{jm_id}...")

    try:
        conv = get_converter()
        result = conv.convert(jm_id)

        # Format response based on source
        source_emoji = {"ehentai": "✅", "hitomi": "🔶", "google": "🔍"}

        source_name = {
            "ehentai": "E-Hentai",
            "hitomi": "Hitomi.la (search)",
            "google": "Google (search)",
        }

        emoji = source_emoji.get(result.source, "📎")
        source = source_name.get(result.source, result.source)

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
    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("jm", convert_jm))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    # Run the bot
    logger.info("Starting JM2E Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
