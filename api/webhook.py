"""
Telegram webhook handler for Vercel serverless function.
"""

import json
import os
import re
import httpx
from http.server import BaseHTTPRequestHandler

# Import converter from parent directory
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jm2e import JM2EConverter

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# Lazy-init converter (reused across warm invocations)
_converter = None


def get_converter():
    global _converter
    if _converter is None:
        _converter = JM2EConverter()
    return _converter


def send_message(chat_id: int, text: str, parse_mode: str | None = None):
    """Send message via Telegram API."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    with httpx.Client(timeout=10) as client:
        client.post(
            f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
        )


def handle_message(message: dict):
    """Process incoming Telegram message."""
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return

    # Handle /start command
    if text == "/start":
        send_message(
            chat_id,
            "Welcome to JM2E Bot!\n\n"
            "Send me a JMComic ID (e.g., `540930` or `/jm 540930`) "
            "and I'll find the E-Hentai or wnacg link for you.",
            parse_mode="Markdown",
        )
        return

    # Handle /help command
    if text == "/help":
        send_message(
            chat_id,
            "*JM2E Bot Help*\n\n"
            "• Send a JMComic ID directly: `540930`\n"
            "• Or use command: `/jm 540930`\n\n"
            "The bot will search E-Hentai first, then wnacg as fallback.",
            parse_mode="Markdown",
        )
        return

    # Extract JM ID from text
    jm_id = None

    # Pattern: /jm <id> or /jm<id>
    match = re.match(r"^/jm\s*(\d+)$", text, re.IGNORECASE)
    if match:
        jm_id = match.group(1)

    # Pattern: just a number
    if not jm_id and re.match(r"^\d{5,7}$", text):
        jm_id = text

    # Pattern: JM<id> or jm<id>
    if not jm_id:
        match = re.search(r"\bjm(\d{5,7})\b", text, re.IGNORECASE)
        if match:
            jm_id = match.group(1)

    if not jm_id:
        send_message(
            chat_id,
            "Please send a valid JMComic ID (5-7 digits).\n"
            "Example: `540930` or `/jm 540930`",
            parse_mode="Markdown",
        )
        return

    # Process conversion
    send_message(chat_id, f"🔍 Looking up JM{jm_id}...")

    try:
        converter = get_converter()
        result = converter.convert(jm_id)

        if result.link:
            source_emoji = "📚" if result.source == "ehentai" else "📖"
            response = (
                f"{source_emoji} *{result.source.upper()}*\n\n"
                f"Title: {result.title[:100]}{'...' if len(result.title) > 100 else ''}\n\n"
                f"[Open Link]({result.link})"
            )
        else:
            response = (
                f"❌ No link found for JM{jm_id}\n\n"
                f"Title: {result.title[:100]}{'...' if len(result.title) > 100 else ''}\n\n"
                "This might be ExHentai-only, JM-exclusive, or not yet uploaded elsewhere."
            )

        send_message(chat_id, response, parse_mode="Markdown")

    except Exception as e:
        send_message(chat_id, f"❌ Error: {str(e)[:200]}")


class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler."""

    def do_POST(self):
        """Handle POST request from Telegram webhook."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            update = json.loads(body.decode("utf-8"))

            # Process message
            message = update.get("message")
            if message:
                handle_message(message)

            # Always return 200 to Telegram
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        except Exception as e:
            print(f"Error: {e}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"JM2E Bot is running!")
