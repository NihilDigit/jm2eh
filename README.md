# JM2E Bot

Telegram bot that converts JMComic IDs to E-Hentai / wnacg links.

**Bot**: [@jm2eh_bot](https://t.me/jm2eh_bot)

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

## Self-Hosting

### Prerequisites

- Python 3.12+
- Vercel account (for serverless deployment)

### Deploy to Vercel

1. Fork this repository
2. Import to Vercel
3. Add environment variable: `TELEGRAM_TOKEN`
4. Deploy
5. Set webhook:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<YOUR_DOMAIN>/webhook"
   ```

### Local Development

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
