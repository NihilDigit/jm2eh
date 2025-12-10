"""
Telegram webhook handler for Vercel serverless function.
"""

import json
import os
import re
import httpx
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import Optional

# Import converter from parent directory
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jm2e import JM2EConverter

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

# Vercel Edge Config (for persistent storage)
# Supports both Vercel KV (legacy) and Edge Config
EDGE_CONFIG = os.environ.get("EDGE_CONFIG", "")  # Connection string with token
EDGE_CONFIG_ID = os.environ.get("EDGE_CONFIG_ID", "")
VERCEL_API_TOKEN = os.environ.get("VERCEL_API_TOKEN", "")
VERCEL_TEAM_ID = os.environ.get("VERCEL_TEAM_ID", "")

# Legacy Vercel KV support (fallback)
KV_REST_API_URL = os.environ.get("KV_REST_API_URL", "")
KV_REST_API_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

# Lazy-init converters (reused across warm invocations)
# Key: cookie hash, Value: converter instance
_converters: dict[str, JM2EConverter] = {}

# User cookie storage (in-memory cache, may reset on cold start)
_user_cookies: dict[int, str] = {}

# User persistence preference (in-memory cache)
_user_persist: dict[int, bool] = {}

# User blur preference (in-memory cache, default True = blur enabled)
_user_blur: dict[int, bool] = {}

# User wnacg-only preference (in-memory cache, default False)
_user_wnacg_only: dict[int, bool] = {}


# ============== Storage Helper Functions (Edge Config / KV) ==============


def kv_available() -> bool:
    """Check if persistent storage is configured (Edge Config or KV)."""
    # Prefer Edge Config
    if EDGE_CONFIG and EDGE_CONFIG_ID and VERCEL_API_TOKEN:
        return True
    # Fallback to legacy KV
    return bool(KV_REST_API_URL and KV_REST_API_TOKEN)


def _edge_config_read(key: str) -> Optional[str]:
    """Read from Edge Config."""
    if not EDGE_CONFIG:
        return None

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{EDGE_CONFIG.split('?')[0]}/item/{key}?{EDGE_CONFIG.split('?')[1]}"
            )
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception:
        return None


def _edge_config_write(items: dict) -> bool:
    """Write to Edge Config via Vercel API.

    Args:
        items: Dict of key-value pairs to upsert
    """
    if not (EDGE_CONFIG_ID and VERCEL_API_TOKEN):
        return False

    try:
        url = f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items"
        if VERCEL_TEAM_ID:
            url += f"?teamId={VERCEL_TEAM_ID}"

        payload = {
            "items": [
                {"operation": "upsert", "key": k, "value": v} for k, v in items.items()
            ]
        }

        with httpx.Client(timeout=10) as client:
            resp = client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {VERCEL_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            return resp.status_code == 200
    except Exception:
        return False


def _edge_config_delete(keys: list[str]) -> bool:
    """Delete from Edge Config via Vercel API."""
    if not (EDGE_CONFIG_ID and VERCEL_API_TOKEN):
        return False

    try:
        url = f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items"
        if VERCEL_TEAM_ID:
            url += f"?teamId={VERCEL_TEAM_ID}"

        payload = {"items": [{"operation": "delete", "key": k} for k in keys]}

        with httpx.Client(timeout=10) as client:
            resp = client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {VERCEL_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            return resp.status_code == 200
    except Exception:
        return False


def kv_get(key: str) -> Optional[str]:
    """Get value from storage."""
    # Try Edge Config first
    if EDGE_CONFIG:
        return _edge_config_read(key)

    # Fallback to legacy KV
    if not (KV_REST_API_URL and KV_REST_API_TOKEN):
        return None

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{KV_REST_API_URL}/get/{key}",
                headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"},
            )
            data = resp.json()
            result = data.get("result")
            return result if result else None
    except Exception:
        return None


def kv_set(key: str, value: str, ex: int | None = None) -> bool:
    """Set value in storage."""
    # Try Edge Config first
    if EDGE_CONFIG and EDGE_CONFIG_ID and VERCEL_API_TOKEN:
        return _edge_config_write({key: value})

    # Fallback to legacy KV
    if not (KV_REST_API_URL and KV_REST_API_TOKEN):
        return False

    try:
        url = f"{KV_REST_API_URL}/set/{key}/{value}"
        if ex:
            url += f"?ex={ex}"

        with httpx.Client(timeout=5) as client:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


def kv_delete(key: str) -> bool:
    """Delete key from storage."""
    # Try Edge Config first
    if EDGE_CONFIG and EDGE_CONFIG_ID and VERCEL_API_TOKEN:
        return _edge_config_delete([key])

    # Fallback to legacy KV
    if not (KV_REST_API_URL and KV_REST_API_TOKEN):
        return False

    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(
                f"{KV_REST_API_URL}/del/{key}",
                headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"},
            )
            return resp.status_code == 200
    except Exception:
        return False


# ============== User Data Management ==============


def get_user_cookie(user_id: int) -> Optional[str]:
    """Get user's ExHentai cookie (from cache or KV)."""
    # Check in-memory cache first
    if user_id in _user_cookies:
        return _user_cookies[user_id]

    # Try to load from KV if user has persistence enabled
    if kv_available():
        persist = kv_get(f"user_{user_id}_persist")
        if persist == "1":
            cookie = kv_get(f"user_{user_id}_cookie")
            if cookie:
                _user_cookies[user_id] = cookie
                _user_persist[user_id] = True
                return cookie

    return None


def set_user_cookie(user_id: int, cookie: str) -> None:
    """Set user's ExHentai cookie."""
    _user_cookies[user_id] = cookie

    # If user has persistence enabled, save to KV
    if _user_persist.get(user_id) and kv_available():
        kv_set(f"user_{user_id}_cookie", cookie)


def delete_user_cookie(user_id: int) -> None:
    """Delete user's ExHentai cookie."""
    if user_id in _user_cookies:
        del _user_cookies[user_id]

    # Also delete from KV if available
    if kv_available():
        kv_delete(f"user_{user_id}_cookie")


def get_user_persist(user_id: int) -> bool:
    """Check if user has persistence enabled."""
    if user_id in _user_persist:
        return _user_persist[user_id]

    if kv_available():
        persist = kv_get(f"user_{user_id}_persist")
        result = persist == "1"
        _user_persist[user_id] = result
        return result

    return False


def set_user_persist(user_id: int, enabled: bool) -> bool:
    """Enable or disable persistence for user.

    Returns True if successful.
    """
    if not kv_available():
        return False

    _user_persist[user_id] = enabled

    if enabled:
        # Write persist flag
        if not kv_set(f"user_{user_id}_persist", "1"):
            return False
        # Also persist current cookie if exists
        if user_id in _user_cookies:
            if not kv_set(f"user_{user_id}_cookie", _user_cookies[user_id]):
                return False
    else:
        kv_delete(f"user_{user_id}_persist")
        kv_delete(f"user_{user_id}_cookie")

    return True


def delete_all_user_data(user_id: int) -> None:
    """Delete all user data (cookie + persistence setting + blur setting + wnacg_only)."""
    if user_id in _user_cookies:
        del _user_cookies[user_id]
    if user_id in _user_persist:
        del _user_persist[user_id]
    if user_id in _user_blur:
        del _user_blur[user_id]
    if user_id in _user_wnacg_only:
        del _user_wnacg_only[user_id]

    if kv_available():
        kv_delete(f"user_{user_id}_cookie")
        kv_delete(f"user_{user_id}_persist")
        kv_delete(f"user_{user_id}_blur")
        kv_delete(f"user_{user_id}_wnacg_only")


def get_user_blur(user_id: int) -> bool:
    """Get user's blur preference. Default is True (blur enabled)."""
    if user_id in _user_blur:
        return _user_blur[user_id]

    if kv_available():
        blur = kv_get(f"user_{user_id}_blur")
        if blur is not None:
            result = blur != "0"  # "0" means disabled
            _user_blur[user_id] = result
            return result

    return True  # Default: blur enabled


def set_user_blur(user_id: int, enabled: bool) -> None:
    """Set user's blur preference."""
    _user_blur[user_id] = enabled

    # If user has persistence enabled, save to KV
    if _user_persist.get(user_id) and kv_available():
        kv_set(f"user_{user_id}_blur", "1" if enabled else "0")


def get_user_wnacg_only(user_id: int) -> bool:
    """Get user's wnacg-only preference. Default is False."""
    if user_id in _user_wnacg_only:
        return _user_wnacg_only[user_id]

    if kv_available():
        wnacg_only = kv_get(f"user_{user_id}_wnacg_only")
        if wnacg_only is not None:
            result = wnacg_only == "1"
            _user_wnacg_only[user_id] = result
            return result

    return False  # Default: wnacg-only disabled


def set_user_wnacg_only(user_id: int, enabled: bool) -> None:
    """Set user's wnacg-only preference."""
    _user_wnacg_only[user_id] = enabled

    # If user has persistence enabled, save to KV
    if _user_persist.get(user_id) and kv_available():
        kv_set(f"user_{user_id}_wnacg_only", "1" if enabled else "0")


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
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Send message via Telegram API.

    Returns the message_id of the sent message, or None on failure.
    """
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_preview:
        payload["disable_web_page_preview"] = True
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("result", {}).get("message_id")
    except Exception:
        pass
    return None


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


def set_my_commands():
    """Set bot commands for the menu button.

    This creates the slash command menu that appears when users type '/'.
    """
    commands = [
        {"command": "start", "description": "开始使用 / 查看引导"},
        {"command": "jm", "description": "转换 JM ID (例: /jm 540930)"},
        {"command": "setcookie", "description": "设置 ExHentai Cookie"},
        {"command": "status", "description": "查看当前状态"},
        {"command": "blur", "description": "切换封面模糊"},
        {"command": "persist", "description": "启用云端存储"},
        {"command": "forget", "description": "删除所有数据"},
        {"command": "help", "description": "显示帮助信息"},
    ]

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/setMyCommands",
                json={"commands": commands},
            )
            return resp.status_code == 200
    except Exception:
        return False


def send_chat_action(chat_id: int, action: str = "typing"):
    """Send chat action (typing indicator, etc.).

    Available actions:
    - typing: for text messages
    - upload_photo: for photos
    - upload_document: for files
    - find_location: for location data
    """
    try:
        with httpx.Client(timeout=5) as client:
            client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
            )
    except Exception:
        pass  # Non-critical, ignore errors


def set_message_reaction(
    chat_id: int, message_id: int | None, emoji: str, is_big: bool = False
):
    """Set reaction on a message.

    Popular emoji reactions: 👍 👎 ❤️ 🔥 🎉 😢 💯 👀 🤔 🤯
    """
    if message_id is None:
        return

    try:
        with httpx.Client(timeout=5) as client:
            client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": is_big,
                },
            )
    except Exception:
        pass  # Reactions may not be available in all chats


def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode: str | None = None,
    disable_preview: bool = False,
    reply_markup: dict | None = None,
):
    """Edit an existing message."""
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_preview:
        payload["disable_web_page_preview"] = True
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        with httpx.Client(timeout=10) as client:
            client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/editMessageText",
                json=payload,
            )
    except Exception:
        pass  # Fall back to sending new message if edit fails


def send_photo(
    chat_id: int,
    photo_url: str,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_to_message_id: int | None = None,
    reply_markup: dict | None = None,
    has_spoiler: bool = False,
) -> int | None:
    """Send photo via Telegram API.

    Args:
        chat_id: Chat to send to
        photo_url: URL of the photo to send
        caption: Optional caption for the photo
        parse_mode: Optional parse mode (HTML, Markdown, etc.)
        reply_to_message_id: Optional message to reply to
        reply_markup: Optional inline keyboard
        has_spoiler: If True, photo will be blurred until user clicks

    Returns the message_id of the sent message, or None on failure.
    """
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if has_spoiler:
        payload["has_spoiler"] = True

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendPhoto",
                json=payload,
            )
            data = resp.json()
            if data.get("ok"):
                return data.get("result", {}).get("message_id")
    except Exception:
        pass
    return None


def edit_message_media(
    chat_id: int,
    message_id: int,
    photo_url: str,
    caption: str | None = None,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Edit an existing message to show a photo with caption.

    Returns True if successful.
    """
    media = {"type": "photo", "media": photo_url}
    if caption:
        media["caption"] = caption
    if parse_mode:
        media["parse_mode"] = parse_mode

    payload = {"chat_id": chat_id, "message_id": message_id, "media": media}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/editMessageMedia",
                json=payload,
            )
            return resp.json().get("ok", False)
    except Exception:
        return False


def escape_html(text: str) -> str:
    """Escape special characters for Telegram HTML.

    Characters that need escaping: < > &
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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

    # Get user's ExHentai cookie if set (from cache or KV)
    user_cookie = get_user_cookie(user_id)
    user_has_persist = get_user_persist(user_id)

    # Handle /start command - Onboarding flow
    if text == "/start":
        # Set bot commands menu (do this once on start)
        set_my_commands()

        if user_cookie:
            # Returning user with cookie set
            persist_info = "☁️ 云端保存" if user_has_persist else "💾 本地缓存"
            send_message(
                chat_id,
                f"👋 <b>欢迎回来！</b>\n\n"
                f"✅ ExHentai Cookie 已设置\n"
                f"📦 存储状态: {persist_info}\n\n"
                f"直接发送 JM ID 即可查询，例如:\n"
                f"<code>540930</code>",
                parse_mode="HTML",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "📊 查看状态", "callback_data": "status"},
                            {"text": "❓ 帮助", "callback_data": "help"},
                        ]
                    ]
                },
            )
        else:
            # New user - show onboarding
            send_message(
                chat_id,
                "🔗 <b>JM2E Bot</b>\n"
                "<i>JMComic → E-Hentai/ExHentai 链接转换</i>\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "📖 <b>使用方法</b>\n"
                "直接发送 JMComic ID 即可查询对应链接\n\n"
                "💡 <b>示例</b>\n"
                "<code>540930</code> 或 <code>/jm 540930</code>\n\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "🔍 <b>搜索顺序</b>\n"
                "1. E-Hentai (默认)\n"
                "2. wnacg (备选)\n\n"
                "🔞 <b>解锁 ExHentai</b>\n"
                "设置 Cookie 后可搜索 ExHentai，找到更多内容\n\n"
                "🖼️ <b>封面模糊</b>\n"
                "默认开启，使用 /blur 切换",
                parse_mode="HTML",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🍪 设置 Cookie", "callback_data": "guide_cookie"},
                        ],
                        [
                            {"text": "🚀 直接开始使用", "callback_data": "dismiss"},
                            {"text": "❓ 帮助", "callback_data": "help"},
                        ],
                    ]
                },
            )
        return

    # Handle /help command
    if text == "/help":
        cloud_section = (
            "\n<b>☁️ 云端存储</b>\n/persist - 启用云端存储\n/forget - 删除所有数据\n"
            if kv_available()
            else ""
        )
        send_message(
            chat_id,
            "📖 <b>JM2E Bot 帮助</b>\n\n"
            "<b>🔍 基本用法</b>\n"
            "• 直接发送 ID: <code>540930</code>\n"
            "• 使用命令: <code>/jm 540930</code>\n"
            "• 粘贴 JMComic 链接\n\n"
            "<b>📋 命令列表</b>\n"
            "/start - 开始使用\n"
            "/jm &lt;id&gt; - 转换 JM ID\n"
            "/status - 查看当前状态\n"
            "/setcookie - 设置 Cookie\n"
            f"{cloud_section}\n"
            "<b>🍪 设置 Cookie</b>\n"
            "直接粘贴 Cookie，或:\n"
            "<code>/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx</code>",
            parse_mode="HTML",
        )
        return

    # Handle /status command
    if text == "/status":
        cookie_status = "✅ 已设置" if user_cookie else "❌ 未设置"
        wnacg_only = get_user_wnacg_only(user_id)
        if wnacg_only:
            search_order = "wnacg only"
        elif user_cookie:
            search_order = "ExHentai → wnacg"
        else:
            search_order = "E-Hentai → wnacg"
        blur_enabled = get_user_blur(user_id)
        blur_status = "🔒 已开启" if blur_enabled else "🔓 已关闭"
        wnacg_status = "📗 已开启" if wnacg_only else "❌ 已关闭"

        if kv_available():
            persist_status = "☁️ 已启用" if user_has_persist else "💾 仅本地"
            persist_hint = "(已云端保存)" if user_has_persist else "(重启可能丢失)"
        else:
            persist_status = "⚠️ 不可用"
            persist_hint = ""

        send_message(
            chat_id,
            f"📊 <b>当前状态</b>\n\n"
            f"🍪 Cookie: {cookie_status}\n"
            f"🔍 搜索顺序: {search_order}\n"
            f"📗 WNACG-only: {wnacg_status}\n"
            f"🖼️ 封面模糊: {blur_status}\n"
            f"☁️ 云端存储: {persist_status} {persist_hint}",
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "🍪 设置 Cookie", "callback_data": "guide_cookie"},
                        {"text": "☁️ 启用云存储", "callback_data": "persist"},
                    ]
                ]
            }
            if not user_cookie and not user_has_persist
            else None,
        )
        return

    # Handle /blur command (toggle cover blur)
    if text == "/blur":
        blur_enabled = get_user_blur(user_id)
        new_blur = not blur_enabled
        set_user_blur(user_id, new_blur)

        if new_blur:
            send_message(
                chat_id,
                "🔒 封面模糊已<b>开启</b>\n\n点击图片可查看原图。",
                parse_mode="HTML",
            )
        else:
            send_message(
                chat_id,
                "🔓 封面模糊已<b>关闭</b>\n\n封面将直接显示。",
                parse_mode="HTML",
            )
        return

    # Handle /wnacg command (toggle wnacg-only mode)
    if text == "/wnacg":
        wnacg_only = get_user_wnacg_only(user_id)
        new_wnacg_only = not wnacg_only
        set_user_wnacg_only(user_id, new_wnacg_only)

        if new_wnacg_only:
            send_message(
                chat_id,
                "📗 <b>WNACG-only 模式已开启</b>\n\n跳过 E-Hentai，只搜索绅士漫画。",
                parse_mode="HTML",
            )
        else:
            send_message(
                chat_id,
                "🔄 <b>WNACG-only 模式已关闭</b>\n\n恢复正常搜索顺序。",
                parse_mode="HTML",
            )
        return

    # Handle /persist command (enable cloud storage)
    if text == "/persist":
        if not kv_available():
            send_message(
                chat_id,
                "⚠️ 云端存储不可用\n\n服务器未配置存储后端。",
            )
            return

        if user_has_persist:
            send_message(
                chat_id,
                "☁️ 云端存储已启用\n\n你的cookie已在云端保存，重启不会丢失。",
            )
            return

        if not user_cookie:
            send_message(
                chat_id,
                "❌ 请先设置cookie\n\n使用 /setcookie 设置后再启用云端存储。",
            )
            return

        if set_user_persist(user_id, True):
            send_message(
                chat_id,
                "✅ 云端存储已启用\\!\n\n"
                "你的cookie已保存到云端，即使服务器重启也不会丢失。\n\n"
                "使用 /forget 可随时删除云端数据。",
                parse_mode="MarkdownV2",
            )
        else:
            send_message(chat_id, "❌ 启用失败，请稍后重试。")
        return

    # Handle /forget command (delete all cloud data)
    if text == "/forget":
        if not kv_available():
            send_message(
                chat_id,
                "⚠️ 云端存储不可用",
            )
            return

        delete_all_user_data(user_id)
        send_message(
            chat_id,
            "🗑️ 已删除所有数据\n\n"
            "• 云端cookie已删除\n"
            "• 云端存储已禁用\n"
            "• 本地缓存已清除\n\n"
            "如需继续使用ExHentai，请重新设置cookie。",
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
            set_user_cookie(user_id, cookie)

            # Suggest enabling cloud storage
            persist_hint = ""
            if kv_available() and not user_has_persist:
                persist_hint = "\n\n💡 使用 /persist 可启用云端存储，重启不丢失。"

            send_message(
                chat_id,
                f"✅ Cookie验证成功!\n\n"
                f"搜索将优先使用ExHentai。\n"
                f"为安全起见，您的cookie消息已删除。{persist_hint}",
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

    # React to the message to show we received it
    set_message_reaction(chat_id, message_id, "👀")

    # Show typing indicator
    send_chat_action(chat_id, "typing")

    try:
        converter = get_converter(user_cookie)
        wnacg_only = get_user_wnacg_only(user_id)
        result = converter.convert(jm_id, wnacg_only=wnacg_only)

        if result.link:
            # Success! Update reaction
            set_message_reaction(chat_id, message_id, "🔥")

            source_emoji = {"exhentai": "🔞", "ehentai": "✅", "wnacg": "📗"}.get(
                result.source, "📎"
            )
            source_name = {
                "exhentai": "ExHentai",
                "ehentai": "E-Hentai",
                "wnacg": "绅士漫画",
            }.get(result.source, result.source)

            # Escape HTML special chars in title/author
            title_raw = result.title[:80] + ("..." if len(result.title) > 80 else "")
            title_display = escape_html(title_raw)
            author_display = escape_html(result.author)

            # Use HTML format - no complex escaping needed
            response = (
                f"{source_emoji} <b>JM{jm_id}</b>\n\n"
                f"📚 {title_display}\n"
                f"✍️ {author_display}\n"
                f"🔗 {source_name}"
            )

            # Create inline keyboard with useful buttons
            inline_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🔗 打开链接", "url": result.link},
                        {
                            "text": "📋 JMComic",
                            "url": f"https://18comic.vip/album/{jm_id}",
                        },
                    ]
                ]
            }

            # Try to send with cover image if available
            photo_sent = False
            if result.cover_url:
                blur_enabled = get_user_blur(user_id)
                photo_msg_id = send_photo(
                    chat_id,
                    result.cover_url,
                    caption=response,
                    parse_mode="HTML",
                    reply_to_message_id=message_id,
                    reply_markup=inline_keyboard,
                    has_spoiler=blur_enabled,
                )
                photo_sent = photo_msg_id is not None

            # Fallback to text message if photo failed
            if not photo_sent:
                send_message(
                    chat_id,
                    response,
                    parse_mode="HTML",
                    disable_preview=True,
                    reply_to_message_id=message_id,
                    reply_markup=inline_keyboard,
                )
        else:
            # Not found, sad reaction
            set_message_reaction(chat_id, message_id, "😢")

            title_raw = result.title[:80] + ("..." if len(result.title) > 80 else "")
            title_display = escape_html(title_raw)
            author_display = escape_html(result.author)

            # Use HTML format
            response = (
                f"❌ <b>JM{jm_id}</b>\n\n"
                f"📚 {title_display}\n"
                f"✍️ {author_display}\n\n"
                "未找到匹配的画廊。"
            )
            if not user_cookie:
                response += "\n\n💡 提示: 设置ExHentai cookie可能找到更多结果。"

            # Add a button to search manually (URL encode title for search)
            search_query = urllib.parse.quote(f"{result.title} site:e-hentai.org")
            inline_keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "🔍 Google搜索",
                            "url": f"https://www.google.com/search?q={search_query}",
                        },
                        {
                            "text": "📋 JMComic",
                            "url": f"https://18comic.vip/album/{jm_id}",
                        },
                    ]
                ]
            }

            # Try to send with cover image if available
            photo_sent = False
            if result.cover_url:
                blur_enabled = get_user_blur(user_id)
                photo_msg_id = send_photo(
                    chat_id,
                    result.cover_url,
                    caption=response,
                    parse_mode="HTML",
                    reply_to_message_id=message_id,
                    reply_markup=inline_keyboard,
                    has_spoiler=blur_enabled,
                )
                photo_sent = photo_msg_id is not None

            # Fallback to text message if photo failed
            if not photo_sent:
                send_message(
                    chat_id,
                    response,
                    parse_mode="HTML",
                    disable_preview=True,
                    reply_to_message_id=message_id,
                    reply_markup=inline_keyboard,
                )

    except Exception as e:
        # Error reaction
        set_message_reaction(chat_id, message_id, "👎")

        error_msg = str(e)[:150]
        response = f"❌ 查询出错\n\nJM{jm_id}: {error_msg}\n\n请稍后重试。"

        send_message(chat_id, response, reply_to_message_id=message_id)


def handle_inline_query(inline_query: dict):
    """Handle inline query for quick JM ID lookup.

    Users can type @botname 540930 in any chat to get results.
    """
    query_id = inline_query.get("id")
    query_text = inline_query.get("query", "").strip()
    user_id = inline_query.get("from", {}).get("id")

    if not query_id:
        return

    # Get user's cookie if available
    user_cookie = _user_cookies.get(user_id)

    # Try to extract JM ID
    jm_id = extract_jm_id(query_text) if query_text else None

    results = []

    if jm_id:
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

                title_raw = result.title[:60] + (
                    "..." if len(result.title) > 60 else ""
                )
                title_display = escape_html(title_raw)
                author_display = escape_html(result.author)

                # Create article result with thumbnail (use HTML format)
                article_result = {
                    "type": "article",
                    "id": f"jm_{jm_id}_found",
                    "title": f"{source_emoji} JM{jm_id}",
                    "description": f"{title_raw} - {result.author}",
                    "input_message_content": {
                        "message_text": (
                            f"{source_emoji} <b>JM{jm_id}</b>\n\n"
                            f"📚 {title_display}\n"
                            f"✍️ {author_display}\n"
                            f"🔗 {source_name}"
                        ),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {"text": "🔗 打开链接", "url": result.link},
                                {
                                    "text": "📋 JMComic",
                                    "url": f"https://18comic.vip/album/{jm_id}",
                                },
                            ]
                        ]
                    },
                }
                # Add thumbnail if cover URL is available
                if result.cover_url:
                    article_result["thumbnail_url"] = result.cover_url

                results.append(article_result)
            else:
                # Not found
                title_raw = result.title[:60] + (
                    "..." if len(result.title) > 60 else ""
                )
                title_display = escape_html(title_raw)
                author_display = escape_html(result.author)

                article_result = {
                    "type": "article",
                    "id": f"jm_{jm_id}_notfound",
                    "title": f"❌ JM{jm_id} - 未找到",
                    "description": f"{title_raw} - 无匹配画廊",
                    "input_message_content": {
                        "message_text": (
                            f"❌ <b>JM{jm_id}</b>\n\n"
                            f"📚 {title_display}\n"
                            f"✍️ {author_display}\n\n"
                            "未找到匹配的画廊。"
                        ),
                        "parse_mode": "HTML",
                    },
                }
                # Add thumbnail if cover URL is available
                if result.cover_url:
                    article_result["thumbnail_url"] = result.cover_url

                results.append(article_result)
        except Exception as e:
            results.append(
                {
                    "type": "article",
                    "id": f"jm_{jm_id}_error",
                    "title": f"❌ JM{jm_id} - 查询出错",
                    "description": str(e)[:50],
                    "input_message_content": {
                        "message_text": f"❌ 查询 JM{jm_id} 时出错: {str(e)[:100]}",
                    },
                }
            )
    else:
        # No valid JM ID, show help
        results.append(
            {
                "type": "article",
                "id": "help",
                "title": "🔍 输入JMComic ID",
                "description": "例如: 540930 或 jm540930",
                "input_message_content": {
                    "message_text": (
                        "🔗 <b>JM2E Bot</b>\n\n"
                        "使用方法: <code>@bot_username &lt;JM ID&gt;</code>\n"
                        "例如: <code>@bot_username 540930</code>"
                    ),
                    "parse_mode": "HTML",
                },
            }
        )

    # Send answer
    try:
        with httpx.Client(timeout=30) as client:
            client.post(
                f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/answerInlineQuery",
                json={
                    "inline_query_id": query_id,
                    "results": results,
                    "cache_time": 300,  # Cache for 5 minutes
                    "is_personal": True,  # Results may vary by user (cookie)
                },
            )
    except Exception:
        pass


def handle_callback_query(callback_query: dict):
    """Handle callback query from inline keyboard buttons."""
    query_id = callback_query.get("id")
    data = callback_query.get("data", "")
    user_id = callback_query.get("from", {}).get("id")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    message_id = callback_query.get("message", {}).get("message_id")

    if not query_id or not chat_id:
        return

    # Answer callback to remove loading state
    def answer_callback(text: str = "", show_alert: bool = False):
        try:
            with httpx.Client(timeout=5) as client:
                client.post(
                    f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                    json={
                        "callback_query_id": query_id,
                        "text": text,
                        "show_alert": show_alert,
                    },
                )
        except Exception:
            pass

    if data == "help":
        answer_callback()
        # Send help message
        cloud_section = (
            "\n<b>☁️ 云端存储</b>\n/persist - 启用云端存储\n/forget - 删除所有数据\n"
            if kv_available()
            else ""
        )
        send_message(
            chat_id,
            "📖 <b>JM2E Bot 帮助</b>\n\n"
            "<b>🔍 基本用法</b>\n"
            "• 直接发送 ID: <code>540930</code>\n"
            "• 使用命令: <code>/jm 540930</code>\n"
            "• 粘贴 JMComic 链接\n\n"
            "<b>📋 命令列表</b>\n"
            "/start - 开始使用\n"
            "/jm &lt;id&gt; - 转换 JM ID\n"
            "/status - 查看当前状态\n"
            "/setcookie - 设置 Cookie\n"
            f"{cloud_section}\n"
            "<b>🍪 设置 Cookie</b>\n"
            "直接粘贴 Cookie，或:\n"
            "<code>/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx</code>",
            parse_mode="HTML",
        )

    elif data == "guide_cookie":
        answer_callback()
        send_message(
            chat_id,
            "🍪 <b>设置 ExHentai Cookie</b>\n\n"
            "<b>获取方法:</b>\n"
            "1. 登录 exhentai.org\n"
            "2. 按 F12 打开开发者工具\n"
            "3. 进入 Application → Cookies\n"
            "4. 复制以下三个值:\n"
            "   • <code>ipb_member_id</code>\n"
            "   • <code>ipb_pass_hash</code>\n"
            "   • <code>igneous</code>\n\n"
            "<b>设置方法:</b>\n"
            "直接粘贴 Cookie，格式如:\n"
            "<code>ipb_member_id: xxx\n"
            "ipb_pass_hash: xxx\n"
            "igneous: xxx</code>\n\n"
            "或使用命令:\n"
            "<code>/setcookie ipb_member_id=xxx; ipb_pass_hash=xxx; igneous=xxx</code>",
            parse_mode="HTML",
        )

    elif data == "status":
        answer_callback()
        user_cookie = get_user_cookie(user_id)
        user_has_persist = get_user_persist(user_id)
        cookie_status = "✅ 已设置" if user_cookie else "❌ 未设置"
        search_order = "ExHentai → wnacg" if user_cookie else "E-Hentai → wnacg"
        persist_status = "☁️ 已启用" if user_has_persist else "💾 仅本地"

        send_message(
            chat_id,
            f"📊 <b>当前状态</b>\n\n"
            f"🍪 Cookie: {cookie_status}\n"
            f"🔍 搜索顺序: {search_order}\n"
            f"☁️ 云端存储: {persist_status}",
            parse_mode="HTML",
        )

    elif data == "persist":
        user_cookie = get_user_cookie(user_id)
        user_has_persist = get_user_persist(user_id)

        if user_has_persist:
            answer_callback("☁️ 云端存储已启用", show_alert=False)
        elif not user_cookie:
            answer_callback("❌ 请先设置 Cookie", show_alert=True)
        elif set_user_persist(user_id, True):
            answer_callback("✅ 云端存储已启用！", show_alert=False)
            send_message(
                chat_id,
                "✅ 云端存储已启用！\n\n你的 Cookie 已保存到云端，即使服务器重启也不会丢失。",
            )
        else:
            answer_callback("❌ 启用失败，请稍后重试", show_alert=True)

    elif data == "dismiss":
        answer_callback()
        # Just dismiss, do nothing

    else:
        answer_callback()


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

            # Process inline query
            inline_query = update.get("inline_query")
            if inline_query:
                handle_inline_query(inline_query)

            # Process callback query (button clicks)
            callback_query = update.get("callback_query")
            if callback_query:
                handle_callback_query(callback_query)

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
