# AI-Powered Telegram Trading Bot

This bot uses Google Gemini Vision to analyze trading chart screenshots and provides buy/sell/neutral signals in Bengali.

## Features
- Telegram channel force-join verification
- Supports Binance, Forex, Crypto, Quotex, and TradingView screenshots
- Detects candlestick patterns, trends, support/resistance, liquidity, and related market structure signals
- Returns direct Bengali analysis output

## Prerequisites
1. Telegram bot token from [@BotFather](https://t.me/BotFather)
2. Public Telegram channel username without `@`
3. Gemini API key from [Google AI Studio](https://aistudio.google.com/)

## Deployment on Render
1. Push this repository to GitHub.
2. In Render, create a new Web Service and connect the repository.
3. Render can use the included `render.yaml`, or you can set the same values manually:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python -u bot.py`
   - Python Version: `3.11.9`
4. Add these environment variables in Render:
   - `BOT_TOKEN`
   - `GEMINI_API_KEY`
   - `CHANNEL_USERNAME`
   - `GEMINI_MODEL` optional, default is `gemini-2.5-flash`
   - `ADMIN_USERNAME` optional, default is `admin`
   - `ADMIN_PASSWORD` strongly recommended
   - `FLASK_SECRET_KEY` strongly recommended
5. Deploy the service.

## Important Notes
- `CHANNEL_USERNAME` must be the public channel username without `@`.
- Add the bot to the channel before testing membership checks.
- Channel membership verification works best when the bot is an admin in that channel.
- Render runs this as a web service because the bot starts a small Flask healthcheck server on the assigned `PORT`.
- Deploy URL open করলে admin login page দেখা যাবে. Login করার পর user count, user block/unblock/delete, Gemini API key change, আর channel change করা যাবে.
- `storage/` folder local file-based. Render restart বা redeploy-এর পর data রাখতে চাইলে persistent disk লাগবে.

## Local Testing
```bash
pip install -r requirements.txt
set BOT_TOKEN=your_bot_token
set GEMINI_API_KEY=your_gemini_api_key
set CHANNEL_USERNAME=your_channel_username
python bot.py
```
