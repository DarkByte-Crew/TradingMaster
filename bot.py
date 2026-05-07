import os
import tempfile
import re
import logging
from threading import Thread
from flask import Flask
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- CONFIGURATION ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "").strip().lstrip("@")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip() or "gemini-1.5-flash"

# Ensure channel username starts without @ for correct invite link
CHANNEL_LINK = f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else "https://t.me/"

# Configure Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    VISION_MODEL = genai.GenerativeModel(GEMINI_MODEL_NAME)
else:
    VISION_MODEL = None

# Flask keep-alive server
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "AI Trading Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# ---------- HELPER FUNCTIONS ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def is_user_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user has joined the required channel."""
    try:
        member = await context.bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Membership check failed: {e}")
        return False

async def send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send join channel prompt with button."""
    keyboard = [[InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"🙏 আমাদের চ্যানেলে জয়েন করুন প্রথমে:\n{CHANNEL_LINK}\n\nতারপর /start দিন আবার।",
        reply_markup=reply_markup
    )

def analyze_image_with_gemini(image_path: str) -> str:
    """Send image to Gemini Vision API and return structured analysis."""
    if VISION_MODEL is None:
        logger.error("Gemini model is not configured")
        return None

    prompt = """
আপনি একজন বিশেষজ্ঞ ট্রেডিং অ্যানালিস্ট। প্রদত্ত চার্ট ইমেজ (ক্রিপ্টো, ফরেক্স, স্টক) বিস্তারিত বিশ্লেষণ করুন। ক্যান্ডেলস্টিক প্যাটার্ন, ট্রেন্ড দিক, মোমেন্টাম (বুলিশ/বেয়ারিশ), সাপোর্ট-রেসিস্ট্যান্স, ব্রেকআউট/ফেক ব্রেকআউট, লিকুইডিটি, স্মার্ট মানি কনসেপ্ট, মার্কেট স্ট্রাকচার, কনসলিডেশন, রিজেকশন ক্যান্ডেল, ক্রেতা-বিক্রেতা চাপ, ট্রেন্ড কন্টিনিউয়েশন বা রিভার্সাল সম্ভাবনা চিহ্নিত করুন।

এখন নিচের ফরম্যাটে শুধুমাত্র ডাটা রিটার্ন করুন (অতিরিক্ত কোনো ব্যাখ্যা দেবেন না):

SIGNAL_TYPE: BUY or SELL or NEUTRAL
DIRECTION: উপরে or নিচে or অনিশ্চিত
CONFIDENCE: 0-100 সংখ্যা তারপর % চিহ্ন
MARKET_STRENGTH: Strong Bullish or Strong Bearish or Sideways
ENTRY_BIAS: BUY or SELL or WAIT
ANALYSIS_POINTS:
• পয়েন্ট 1
• পয়েন্ট 2
• পয়েন্ট 3
• পয়েন্ট 4
• পয়েন্ট 5 (কমপক্ষে ৫টি)

সব বিশ্লেষণ বাংলায় লিখুন।
"""
    try:
        with open(image_path, "rb") as img_file:
            image_data = img_file.read()
        response = VISION_MODEL.generate_content([prompt, {"mime_type": "image/jpeg", "data": image_data}])
        return response.text
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        return None

def parse_gemini_response(raw_text: str) -> dict:
    """Parse Gemini output into structured dict for final message."""
    if not raw_text:
        return None
    result = {
        "signal": "NEUTRAL",
        "direction": "অনিশ্চিত",
        "confidence": "0%",
        "market_strength": "Sideways",
        "entry_bias": "WAIT",
        "analysis_points": []
    }
    # Extract using regex fallbacks
    signal_match = re.search(r"SIGNAL_TYPE:\s*(BUY|SELL|NEUTRAL)", raw_text, re.IGNORECASE)
    if signal_match:
        result["signal"] = signal_match.group(1).upper()
    dir_match = re.search(r"DIRECTION:\s*(উপরে|নিচে|অনিশ্চিত)", raw_text)
    if dir_match:
        result["direction"] = dir_match.group(1)
    conf_match = re.search(r"CONFIDENCE:\s*(\d{1,3})%", raw_text)
    if conf_match:
        result["confidence"] = f"{conf_match.group(1)}%"
    strength_match = re.search(r"MARKET_STRENGTH:\s*(Strong Bullish|Strong Bearish|Sideways)", raw_text, re.IGNORECASE)
    if strength_match:
        result["market_strength"] = strength_match.group(1)
    bias_match = re.search(r"ENTRY_BIAS:\s*(BUY|SELL|WAIT)", raw_text, re.IGNORECASE)
    if bias_match:
        result["entry_bias"] = bias_match.group(1).upper()
    # Extract bullet points (lines starting with • or -)
    points = re.findall(r"[•\-]\s*(.+?)(?=\n[•\-]|\n\n|$)", raw_text, re.DOTALL)
    if points:
        result["analysis_points"] = [p.strip() for p in points[:6]]
    else:
        result["analysis_points"] = ["বিশ্লেষণ ডেটা প্রক্রিয়াকরণে সমস্যা, কিন্তু সিগন্যাল উপরে দেখানো হয়েছে।"]
    return result

def format_final_message(data: dict) -> str:
    """Build the final Bengali message as per requirements."""
    signal_emoji = {
        "BUY": "🟢 BUY SIGNAL DETECTED",
        "SELL": "🔴 SELL SIGNAL DETECTED",
        "NEUTRAL": "🟡 NEUTRAL SIGNAL"
    }.get(data["signal"], "🟡 NEUTRAL SIGNAL")
    
    analysis_bullets = "\n".join([f"• {point}" for point in data["analysis_points"]])
    
    message = (
        f"{signal_emoji}\n\n"
        f"📈 সম্ভাব্য দিক:\n{data['direction']}\n\n"
        f"🎯 Confidence:\n{data['confidence']}\n\n"
        f"📌 বিস্তারিত Analysis:\n{analysis_bullets}\n\n"
        f"📊 Market Strength:\n{data['market_strength']}\n\n"
        f"⚡ Entry Bias:\n{data['entry_bias']}"
    )
    return message

# ---------- TELEGRAM HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await is_user_member(user_id, context):
        await update.message.reply_text(
            "✅ স্বাগতম! আপনি চ্যানেলে জয়েন্ট আছেন।\n\n"
            "এখন আপনার ট্রেডিং চার্টের স্ক্রিনশট পাঠান। আমি AI দিয়ে বিশ্লেষণ করে সিগন্যাল দেব।"
        )
    else:
        await send_join_message(update, context)

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Verify membership before processing
    if not await is_user_member(user_id, context):
        await send_join_message(update, context)
        return
    
    # Acknowledge receipt
    processing_msg = await update.message.reply_text("⏳ বিশ্লেষণ চলছে, দয়া করে অপেক্ষা করুন...")
    
    photo_file = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
        await photo_file.download_to_drive(tmp_file.name)
        tmp_path = tmp_file.name
    
    try:
        raw_analysis = analyze_image_with_gemini(tmp_path)
        if not raw_analysis:
            await processing_msg.edit_text("❌ AI বিশ্লেষণ ব্যর্থ হয়েছে। পরে আবার চেষ্টা করুন।")
            return
        
        parsed = parse_gemini_response(raw_analysis)
        final_text = format_final_message(parsed)
        await processing_msg.edit_text(final_text)
    except Exception as e:
        logger.error(f"Handler error: {e}")
        await processing_msg.edit_text("⚠️ একটি অপ্রত্যাশিত ত্রুটি ঘটেছে। অনুগ্রহ করে আবার চেষ্টা করুন।")
    finally:
        # Clean up temp file
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 শুধু চার্টের ছবি পাঠান। আমি ট্রেডিং সিগন্যাল দেব। /start দিয়ে শুরু করুন।")

# ---------- MAIN ----------
def main():
    # Start keep-alive Flask server in background
    Thread(target=run_flask, daemon=True).start()
    
    # Build Telegram Application
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))  # catch unknown commands
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))
    
    logger.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set. Add it as an environment variable.")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set. Add it as an environment variable.")
    if not CHANNEL_USERNAME:
        raise ValueError("CHANNEL_USERNAME is not set. Add it as an environment variable without @.")
    main()
