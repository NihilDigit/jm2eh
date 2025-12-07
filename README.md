# JM2E Bot

Telegram bot that converts JMComic IDs to E-Hentai / wnacg links.

## Usage

Send a JMComic ID to the bot:
- Direct number: `540930`
- Command: `/jm 540930`

The bot will search E-Hentai first, then fallback to wnacg if not found.

## Features

- Multi-query search strategy for high accuracy
- Japanese → Romaji conversion (pykakasi)
- Simplified Chinese → Japanese kanji conversion (OpenCC)
- Katakana → English translation (SimplyTranslate API)
- wnacg fallback for Chinese translations
- Cover preview with spoiler blur (toggleable via `/blur`)
- Cloud storage for settings (optional)

## Self-Hosting (Vercel)

### 1. Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Save the API token (format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 2. Deploy to Vercel

1. Fork this repository
2. Go to [Vercel](https://vercel.com) and create a new project
3. Import your forked repository
4. Add environment variable:
   - `TELEGRAM_TOKEN`: Your bot token from BotFather
5. Deploy

### 3. Set Webhook

After deployment, set the webhook URL:

```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook?url=https://<YOUR_VERCEL_DOMAIN>/api/webhook"
```

Replace:
- `<YOUR_TOKEN>`: Your Telegram bot token
- `<YOUR_VERCEL_DOMAIN>`: Your Vercel deployment URL (e.g., `jm2e.vercel.app`)

### 4. (Optional) Enable Cloud Storage

To persist user settings (cookies, preferences) across cold starts:

1. In Vercel project, go to **Storage** → **Create Database** → **Edge Config**
2. Create a new Edge Config store
3. Connect it to your project
4. Add these environment variables:
   - `EDGE_CONFIG`: Auto-filled by Vercel
   - `EDGE_CONFIG_ID`: Your Edge Config ID (from Edge Config settings)
   - `VERCEL_API_TOKEN`: Create at [Vercel Tokens](https://vercel.com/account/tokens)
   - `VERCEL_TEAM_ID`: (Optional) If using a team account

### Environment Variables Summary

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | ✅ | Bot token from BotFather |
| `EDGE_CONFIG` | ❌ | Edge Config connection string |
| `EDGE_CONFIG_ID` | ❌ | Edge Config store ID |
| `VERCEL_API_TOKEN` | ❌ | Vercel API token for Edge Config writes |
| `VERCEL_TEAM_ID` | ❌ | Team ID (if applicable) |

## Local Development

```bash
# Install dependencies with pixi
pixi install

# Run the converter test
pixi run python jm2e.py

# Run local bot (long polling mode)
pixi run python bot.py
```

## License

MIT
