# JM2E Agent Task Specification

## Objective
Create a Telegram bot that converts JMComic IDs to E-Hentai links with fallback mechanisms.

## Input
- JMComic ID (numeric)

## Output Priority (Fallback Chain)
1. **Primary**: E-Hentai gallery link
2. **Fallback 1**: wnacg link
3. **Fallback 2**: Google search link

## Telegram Bot
- Command: Accept JMComic ID as input
- Response: Return the best available link

## Test Cases (Must All Pass)
JMComic IDs to validate:
- 1180203, 540930, 1192427, 1191862, 224412
- 1190464, 1060422, 1026275, 1186623, 1132672
- 280934, 403551, 259194, 364547, 118648
- 347117, 304642, 265033, 270650

## Success Criteria
All 19 JMComic IDs must resolve to a valid link (E-Hentai preferred, fallback acceptable).

## Tech Stack
- Python 3.11
- pixi for dependency management
- python-telegram-bot for Telegram integration
- Web scraping/API for link resolution
