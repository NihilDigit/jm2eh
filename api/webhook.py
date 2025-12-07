"""
Telegram webhook handler for Vercel serverless function.
"""

import json
import os
import re
import httpx
from http.server import BaseHTTPRequestHandler
from typing import Optional

# Import converter from parent directory
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jm2e import JM2EConverter

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# Lazy-init converters (reused across warm invocations)
# Key: cookie hash, Value: converter instance
_converters: dict[str, JM2EConverter] = {}

# User cookie storage (in-memory, resets on cold start)
# For persistence, consider using a database or Vercel KV
_user_cookies: dict[int, str] = {}


def get_converter(exhentai_cookie: Optional[str] = None) -> JM2EConverter:
    """Get or create converter instance."""
    cache_key = str(hash(exhentai_cookie)) if exhentai_cookie else "default"
    if cache_key not in _converters:
        _converters[cache_key] = JM2EConverter(exhentai_cookie=exhentai_cookie)
    return _converters[cache_key]


def send_message(
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    disable_preview: bool = False,
):
    """Send message via Telegram API."""
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_preview:
        payload["disable_web_page_preview"] = True

    with httpx.Client(timeout=10) as client:
        client.post(
            f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload,
        )


def delete_message(chat_id: int, message_id: int):
    """Delete a message via Telegram API."""
    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
    except Exception:
        pass  # Ignore deletion errors


def normalize_cookie(raw: str) -> Optional[str]:
    """Normalize cookie input to standard format.

    Accepts:
    - Standard: "ipb_member_id=123; ipb_pass_hash=abc; igneous=xyz"
    - Key: value: "ipb_member_id: 123\\nipb_pass_hash: abc"
    """
    if not raw:
        return None

    parts = {}
    lines = raw.replace(";", "\n").split("\n")

    for token in lines:
        token = token.strip()
        if not token:
            continue

        # Try "key: value" format
        if ": " in token:
            key, _, value = token.partition(": ")
            key = key.strip()
            value = value.strip()
            if key and value:
                parts[key] = value
                continue

        # Try "key=value" format
        if "=" in token:
            key, _, value = token.partition("=")
            key = key.strip()
            value = value.strip()
            if key and value:
                parts[key] = value
                continue

    if not parts:
        return None

    return "; ".join(f"{k}={v}" for k, v in parts.items())


def verify_exhentai_cookie(cookie: str) -> bool:
    """Verify ExHentai cookie by making a test request."""
    try:
        from curl_cffi import requests as curl_requests

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": cookie,
        }
        resp = curl_requests.get(
            "https://exhentai.org/",
            headers=headers,
            impersonate="chrome",
            timeout=10,
        )
        # Check for sad panda (invalid cookie)
        if "sad panda" in resp.text.lower() or len(resp.text) < 1000:
            return False
        return True
    except Exception:
        return False


def looks_like_cookie(text: str) -> bool:
    """Check if text looks like an ExHentai cookie."""
    return "ipb_member_id" in text and "ipb_pass_hash" in text


def extract_jm_id(text: str) -> Optional[str]:
    """Extract JMComic ID from various formats."""
    # Pattern: /jm <id> or /jm<id>
    match = re.match(r"^/jm\s*(\d+)$", text, re.IGNORECASE)
    if match:
        return match.group(1)

    # Pattern: just a number (5-7 digits)
    if re.match(r"^\d{5,7}$", text):
        return text

    # Pattern: JM<id> or jm<id>
    match = re.search(r"\bjm[\s\-_]?(\d{5,7})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)

    # Pattern: JMComic URL
    match = re.search(r"jmcomic[^\d]*(\d{5,7})", text, re.IGNORECASE)
    if match:
        return match.group(1)

    # Pattern: album/photo ID in URL
    match = re.search(r"(?:album|photo)[/=](\d{5,7})", text, re.IGNORECASE)
    if match:
        return match.group(1)

    return None


def handle_message(message: dict):
    """Process incoming Telegram message."""
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    message_id = message.get("message_id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return

    # Get user's ExHentai cookie if set
    user_cookie = _user_cookies.get(user_id)

    # Handle /start command
    if text == "/start":
        cookie_status = (
            "✅ ExHentai cookie set" if user_cookie else "❌ No ExHentai cookie"
        )
        send_message(
            chat_id,
            f"🔗 *JM2E Bot* - JMComic to E-Hentai/ExHentai Converter\n\n"
            f"Send me a JMComic ID and I'll find the link for you!\n\n"
            f"*Status:* {cookie_status}\n\n"
            f"*Example:* `540930` or `/jm 540930`\n\n"
            f"*Search priority:*\n"
            f"1. ExHentai (if cookie set)\n"
            f"2. E-Hentai\n"
            f"3. wnacg\n\n"
            f"Use `/setcookie` to enable ExHentai search.",
            parse_mode="Markdown",
        )
        return

    # Handle /help command
    if text == "/help":
        send_message(
            chat_id,
            "📖 *How to use JM2E Bot*\n\n"
            "*Basic usage:*\n"
            "• Send a JMComic ID: `540930`\n"
            "• Or use command: `/jm 540930`\n"
            "• Or paste a JMComic link\n\n"
            "*Commands:*\n"
            "/start - Start the bot\n"
            "/help - Show this help\n"
            "/jm <id> - Convert JMComic ID\n"
            "/setcookie - Set ExHentai cookie\n"
            "/clearcookie - Remove cookie\n"
            "/status - Check settings\n\n"
            "*ExHentai Cookie:*\n"
            "Just paste your cookie directly, or use:\n"
            "`/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx`",
            parse_mode="Markdown",
        )
        return

    # Handle /status command
    if text == "/status":
        cookie_status = "✅ Set" if user_cookie else "❌ Not set"
        search_order = "ExHentai → wnacg" if user_cookie else "E-Hentai → wnacg"
        send_message(
            chat_id,
            f"📊 *Current Settings*\n\n"
            f"ExHentai cookie: {cookie_status}\n"
            f"Search priority: {search_order}",
            parse_mode="Markdown",
        )
        return

    # Handle /clearcookie command
    if text == "/clearcookie":
        if user_id in _user_cookies:
            del _user_cookies[user_id]
            send_message(
                chat_id,
                "🗑️ ExHentai cookie cleared.\n\nSearches will now use E-Hentai.",
                parse_mode="Markdown",
            )
        else:
            send_message(
                chat_id, "ℹ️ No ExHentai cookie was set.", parse_mode="Markdown"
            )
        return

    # Handle /setcookie command or direct cookie paste
    is_setcookie_cmd = text.startswith("/setcookie")
    is_direct_cookie = looks_like_cookie(text) and not text.startswith("/")

    if is_setcookie_cmd or is_direct_cookie:
        if is_setcookie_cmd:
            raw_cookie = text[len("/setcookie") :].strip()
        else:
            raw_cookie = text

        if not raw_cookie:
            send_message(
                chat_id,
                "🍪 *Set ExHentai Cookie*\n\n"
                "*方法1:* 直接粘贴cookie\n"
                "```\n"
                "ipb_member_id: xxx\n"
                "ipb_pass_hash: xxx\n"
                "igneous: xxx\n"
                "```\n\n"
                "*方法2:* 使用命令\n"
                "`/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx; igneous=xxx`\n\n"
                "*获取方法:*\n"
                "1. 登录 exhentai.org\n"
                "2. F12 → Application → Cookies\n"
                "3. 复制上述三个值",
                parse_mode="Markdown",
            )
            return

        cookie = normalize_cookie(raw_cookie)

        if not cookie:
            send_message(
                chat_id,
                "❌ 无法解析cookie\n\n"
                "请使用以下格式之一:\n"
                "• `ipb_member_id=xxx; ipb_pass_hash=xxx`\n"
                "• 或每行一个 `key: value`",
                parse_mode="Markdown",
            )
            return

        # Validate required fields
        required = ["ipb_member_id", "ipb_pass_hash"]
        missing = [f for f in required if f not in cookie]

        if missing:
            send_message(
                chat_id,
                f"❌ 缺少必要字段: `{', '.join(missing)}`\n\n"
                "Cookie必须包含:\n"
                "• `ipb_member_id`\n"
                "• `ipb_pass_hash`",
                parse_mode="Markdown",
            )
            return

        # Delete user's message for security (do this early)
        if message_id:
            delete_message(chat_id, message_id)

        # Verify cookie
        send_message(chat_id, "🔄 正在验证cookie...")

        if verify_exhentai_cookie(cookie):
            _user_cookies[user_id] = cookie
            send_message(
                chat_id,
                "✅ Cookie验证成功!\n\n"
                "搜索将优先使用ExHentai。\n"
                "为安全起见，您的cookie消息已删除。",
                parse_mode="Markdown",
            )
        else:
            send_message(
                chat_id,
                "❌ Cookie验证失败 (sad panda)\n\n"
                "可能原因:\n"
                "• Cookie已过期\n"
                "• Cookie格式错误\n"
                "• 账号被封禁\n\n"
                "请重新从浏览器获取cookie。",
                parse_mode="Markdown",
            )
        return

    # Try to extract JM ID
    jm_id = extract_jm_id(text)

    if not jm_id:
        # Only respond to unknown commands
        if text.startswith("/"):
            send_message(
                chat_id,
                "未知命令。使用 /help 查看可用命令。",
            )
        return

    # Process conversion
    send_message(chat_id, f"🔍 正在查询 JM{jm_id}...")

    try:
        converter = get_converter(user_cookie)
        result = converter.convert(jm_id)

        if result.link:
            source_emoji = {"exhentai": "🔞", "ehentai": "✅", "wnacg": "📗"}.get(
                result.source, "📎"
            )
            source_name = {
                "exhentai": "ExHentai",
                "ehentai": "E-Hentai",
                "wnacg": "绅士漫画",
            }.get(result.source, result.source)

            # Escape markdown special chars in title/author
            title_display = result.title[:80] + (
                "..." if len(result.title) > 80 else ""
            )

            response = (
                f"{source_emoji} *JM{jm_id}*\n\n"
                f"📚 {title_display}\n"
                f"✍️ {result.author}\n"
                f"🔗 {source_name}\n\n"
                f"[👉 打开链接]({result.link})"
            )
        else:
            title_display = result.title[:80] + (
                "..." if len(result.title) > 80 else ""
            )
            response = (
                f"❌ *JM{jm_id}*\n\n"
                f"📚 {title_display}\n"
                f"✍️ {result.author}\n\n"
                "未找到匹配的画廊。"
            )
            if not user_cookie:
                response += "\n\n💡 提示: 设置ExHentai cookie可能找到更多结果。"

        send_message(chat_id, response, parse_mode="Markdown", disable_preview=True)

    except Exception as e:
        error_msg = str(e)[:150]
        send_message(
            chat_id,
            f"❌ 查询出错\n\nJM{jm_id}: {error_msg}\n\n请稍后重试。",
        )


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
