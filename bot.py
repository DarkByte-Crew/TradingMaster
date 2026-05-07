import hmac
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread

from flask import Flask, flash, redirect, render_template, request, session, url_for
from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "storage")))
SETTINGS_PATH = DATA_DIR / "settings.json"
USERS_PATH = DATA_DIR / "users.json"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123").strip() or "admin123"
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-this-secret-key").strip() or "change-this-secret-key"

DEFAULT_SETTINGS = {
    "channel_username": os.environ.get("CHANNEL_USERNAME", "").strip().lstrip("@"),
    "gemini_api_key": os.environ.get("GEMINI_API_KEY", "").strip(),
    "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeStore:
    def __init__(self) -> None:
        self.lock = Lock()
        self.client_cache = None
        self.client_signature = None
        self.last_error = ""
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not SETTINGS_PATH.exists():
            self._write_json(SETTINGS_PATH, DEFAULT_SETTINGS)

        if not USERS_PATH.exists():
            self._write_json(USERS_PATH, {})

    def _read_json(self, path: Path, default):
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _write_json(self, path: Path, data) -> None:
        with path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

    def set_last_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message

    def get_last_error(self) -> str:
        with self.lock:
            return self.last_error

    def get_settings(self) -> dict:
        with self.lock:
            settings = self._read_json(SETTINGS_PATH, DEFAULT_SETTINGS.copy())
            merged = DEFAULT_SETTINGS.copy()
            merged.update(settings)
            if merged != settings:
                self._write_json(SETTINGS_PATH, merged)
            return merged

    def update_settings(self, updates: dict) -> dict:
        with self.lock:
            settings = self._read_json(SETTINGS_PATH, DEFAULT_SETTINGS.copy())
            merged = DEFAULT_SETTINGS.copy()
            merged.update(settings)
            settings = merged
            settings.update(updates)
            settings["channel_username"] = settings.get("channel_username", "").strip().lstrip("@")
            settings["gemini_api_key"] = settings.get("gemini_api_key", "").strip()
            settings["gemini_model"] = settings.get("gemini_model", "").strip() or "gemini-2.5-flash"
            self._write_json(SETTINGS_PATH, settings)
            self.last_error = ""
            return settings

    def get_users(self) -> dict:
        with self.lock:
            return self._read_json(USERS_PATH, {})

    def get_user(self, user_id: int) -> dict | None:
        users = self.get_users()
        return users.get(str(user_id))

    def upsert_user(self, user, increment_photos: bool = False) -> None:
        if user is None:
            return

        with self.lock:
            users = self._read_json(USERS_PATH, {})
            key = str(user.id)
            existing = users.get(key, {})
            users[key] = {
                "user_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "is_bot": user.is_bot,
                "joined_at": existing.get("joined_at", now_iso()),
                "last_seen_at": now_iso(),
                "photos_sent": existing.get("photos_sent", 0) + (1 if increment_photos else 0),
                "blocked": existing.get("blocked", False),
            }
            self._write_json(USERS_PATH, users)

    def set_user_blocked(self, user_id: int, blocked: bool) -> None:
        with self.lock:
            users = self._read_json(USERS_PATH, {})
            key = str(user_id)
            if key in users:
                users[key]["blocked"] = blocked
                users[key]["last_seen_at"] = now_iso()
                self._write_json(USERS_PATH, users)

    def delete_user(self, user_id: int) -> None:
        with self.lock:
            users = self._read_json(USERS_PATH, {})
            key = str(user_id)
            if key in users:
                del users[key]
                self._write_json(USERS_PATH, users)

    def is_user_blocked(self, user_id: int) -> bool:
        user = self.get_user(user_id)
        return bool(user and user.get("blocked"))

    def get_gemini_client(self):
        settings = self.get_settings()
        api_key = settings.get("gemini_api_key", "")

        if not api_key:
            return None

        signature = (api_key,)
        with self.lock:
            if self.client_cache is not None and self.client_signature == signature:
                return self.client_cache

            self.client_cache = genai.Client(api_key=api_key)
            self.client_signature = signature
            return self.client_cache


store = RuntimeStore()
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET_KEY


def is_admin_logged_in() -> bool:
    return bool(session.get("admin_logged_in"))


def require_admin():
    if not is_admin_logged_in():
        return redirect(url_for("login"))
    return None


def get_channel_link() -> str:
    settings = store.get_settings()
    channel_username = settings.get("channel_username", "")
    return f"https://t.me/{channel_username}" if channel_username else "https://t.me/"


@flask_app.route("/healthz")
def healthz():
    return "ok"


@flask_app.route("/")
def root():
    return redirect(url_for("admin_dashboard"))


@flask_app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        valid_username = hmac.compare_digest(username, ADMIN_USERNAME)
        valid_password = hmac.compare_digest(password, ADMIN_PASSWORD)

        if valid_username and valid_password:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))

        flash("Invalid admin credentials.", "error")

    return render_template("login.html")


@flask_app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@flask_app.route("/admin")
def admin_dashboard():
    gate = require_admin()
    if gate:
        return gate

    users = list(store.get_users().values())
    users.sort(key=lambda item: item.get("last_seen_at", ""), reverse=True)
    settings = store.get_settings()

    stats = {
        "total_users": len(users),
        "blocked_users": sum(1 for user in users if user.get("blocked")),
        "photo_senders": sum(1 for user in users if user.get("photos_sent", 0) > 0),
    }

    return render_template(
        "admin.html",
        users=users,
        settings=settings,
        stats=stats,
        admin_username=ADMIN_USERNAME,
        default_password_warning=ADMIN_PASSWORD == "admin123",
        last_error=store.get_last_error(),
    )


@flask_app.route("/admin/settings", methods=["POST"])
def update_settings_route():
    gate = require_admin()
    if gate:
        return gate

    updated = {
        "channel_username": request.form.get("channel_username", "").strip().lstrip("@"),
        "gemini_api_key": request.form.get("gemini_api_key", "").strip(),
        "gemini_model": request.form.get("gemini_model", "gemini-2.5-flash").strip(),
    }
    store.update_settings(updated)
    flash("Settings updated successfully.", "success")
    return redirect(url_for("admin_dashboard"))


@flask_app.route("/admin/users/<int:user_id>/block", methods=["POST"])
def block_user(user_id: int):
    gate = require_admin()
    if gate:
        return gate

    store.set_user_blocked(user_id, True)
    flash(f"User {user_id} blocked.", "success")
    return redirect(url_for("admin_dashboard"))


@flask_app.route("/admin/users/<int:user_id>/unblock", methods=["POST"])
def unblock_user(user_id: int):
    gate = require_admin()
    if gate:
        return gate

    store.set_user_blocked(user_id, False)
    flash(f"User {user_id} unblocked.", "success")
    return redirect(url_for("admin_dashboard"))


@flask_app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id: int):
    gate = require_admin()
    if gate:
        return gate

    store.delete_user(user_id)
    flash(f"User {user_id} removed from records.", "success")
    return redirect(url_for("admin_dashboard"))


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


async def is_user_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = store.get_settings()
    channel_username = settings.get("channel_username", "")

    if not channel_username:
        return True

    try:
        member = await context.bot.get_chat_member(chat_id=f"@{channel_username}", user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as error:
        logger.error("Membership check failed: %s", error)
        return False


async def send_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_link = get_channel_link()
    keyboard = [[
        InlineKeyboardButton("Join Channel", url=channel_link),
        InlineKeyboardButton("আমি জয়েন করেছি", callback_data="check_membership"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if channel_link == "https://t.me/":
        text = "Admin এখনো channel configure করেনি। একটু পরে আবার চেষ্টা করুন।"
    else:
        text = f"আমাদের চ্যানেলে আগে join করুন:\n{channel_link}\n\nJoin করার পর নিচের `আমি জয়েন করেছি` button চাপুন।"

    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def membership_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if update.effective_user:
        store.upsert_user(update.effective_user)

    user_id = update.effective_user.id
    if store.is_user_blocked(user_id):
        await query.edit_message_text("আপনার access admin দ্বারা বন্ধ করা হয়েছে।")
        return

    if await is_user_member(user_id, context):
        await query.edit_message_text(
            "আমাদের চ্যানেলে join হওয়ার জন্য ধন্যবাদ। এখন আপনি bot ব্যবহার করতে পারবেন.\n\nআপনার ট্রেডিং চার্টের screenshot পাঠান। আমি AI দিয়ে analysis করে signal দেব।"
        )
    else:
        channel_link = get_channel_link()
        keyboard = [[
            InlineKeyboardButton("Join Channel", url=channel_link),
            InlineKeyboardButton("আমি জয়েন করেছি", callback_data="check_membership"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"আপনাকে এখনো channel member হিসেবে পাওয়া যায়নি। আগে join করুন:\n{channel_link}\n\nJoin করার পর আবার `আমি জয়েন করেছি` button চাপুন।",
            reply_markup=reply_markup,
        )


def analyze_image_with_gemini(image_path: str) -> str | None:
    client = store.get_gemini_client()
    settings = store.get_settings()
    model_name = settings.get("gemini_model", "gemini-2.5-flash")

    if client is None:
        message = "Gemini API key is not configured."
        store.set_last_error(message)
        logger.error(message)
        return None

    prompt = """
আপনি একজন বিশেষজ্ঞ ট্রেডিং অ্যানালিস্ট। প্রদত্ত চার্ট ইমেজ বিস্তারিত বিশ্লেষণ করুন।

নিচের ফরম্যাটে শুধু data return করুন:

SIGNAL_TYPE: BUY or SELL or NEUTRAL
DIRECTION: উপরে or নিচে or অনিশ্চিত
CONFIDENCE: 0-100%
MARKET_STRENGTH: Strong Bullish or Strong Bearish or Sideways
ENTRY_BIAS: BUY or SELL or WAIT
ANALYSIS_POINTS:
- পয়েন্ট 1
- পয়েন্ট 2
- পয়েন্ট 3
- পয়েন্ট 4
- পয়েন্ট 5

সব বিশ্লেষণ বাংলায় লিখুন।
"""
    try:
        with open(image_path, "rb") as img_file:
            image_data = img_file.read()
        response = client.models.generate_content(
            model=model_name,
            contents=[
                prompt,
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
            ],
        )
        store.set_last_error("")
        return getattr(response, "text", None)
    except Exception as error:
        error_message = str(error)
        store.set_last_error(error_message)
        logger.error("Gemini API error: %s", error_message)
        return None


def parse_gemini_response(raw_text: str) -> dict | None:
    if not raw_text:
        return None

    result = {
        "signal": "NEUTRAL",
        "direction": "অনিশ্চিত",
        "confidence": "0%",
        "market_strength": "Sideways",
        "entry_bias": "WAIT",
        "analysis_points": [],
    }

    signal_match = re.search(r"SIGNAL_TYPE:\s*(BUY|SELL|NEUTRAL)", raw_text, re.IGNORECASE)
    if signal_match:
        result["signal"] = signal_match.group(1).upper()

    dir_match = re.search(r"DIRECTION:\s*(উপরে|নিচে|অনিশ্চিত)", raw_text)
    if dir_match:
        result["direction"] = dir_match.group(1)

    conf_match = re.search(r"CONFIDENCE:\s*(\d{1,3})\s*%?", raw_text)
    if conf_match:
        result["confidence"] = f"{conf_match.group(1)}%"

    strength_match = re.search(r"MARKET_STRENGTH:\s*(Strong Bullish|Strong Bearish|Sideways)", raw_text, re.IGNORECASE)
    if strength_match:
        result["market_strength"] = strength_match.group(1)

    bias_match = re.search(r"ENTRY_BIAS:\s*(BUY|SELL|WAIT)", raw_text, re.IGNORECASE)
    if bias_match:
        result["entry_bias"] = bias_match.group(1).upper()

    points = re.findall(r"[-•]\s*(.+?)(?=\n[-•]|\n\n|$)", raw_text, re.DOTALL)
    if points:
        result["analysis_points"] = [point.strip() for point in points[:6]]
    else:
        result["analysis_points"] = ["বিশ্লেষণ পাওয়া গেছে, কিন্তু bullet points parse করা যায়নি।"]

    return result


def format_final_message(data: dict) -> str:
    signal_label = {
        "BUY": "BUY SIGNAL DETECTED",
        "SELL": "SELL SIGNAL DETECTED",
        "NEUTRAL": "NEUTRAL SIGNAL",
    }.get(data["signal"], "NEUTRAL SIGNAL")

    analysis_bullets = "\n".join([f"- {point}" for point in data["analysis_points"]])

    return (
        f"{signal_label}\n\n"
        f"সম্ভাব্য দিক:\n{data['direction']}\n\n"
        f"Confidence:\n{data['confidence']}\n\n"
        f"বিস্তারিত Analysis:\n{analysis_bullets}\n\n"
        f"Market Strength:\n{data['market_strength']}\n\n"
        f"Entry Bias:\n{data['entry_bias']}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        store.upsert_user(update.effective_user)

    user_id = update.effective_user.id
    if store.is_user_blocked(user_id):
        await update.message.reply_text("আপনার access admin দ্বারা বন্ধ করা হয়েছে।")
        return

    if await is_user_member(user_id, context):
        await update.message.reply_text(
            "আমাদের চ্যানেলে join হওয়ার জন্য ধন্যবাদ। এখন আপনি bot ব্যবহার করতে পারবেন.\n\nআপনার ট্রেডিং চার্টের screenshot পাঠান। আমি AI দিয়ে analysis করে signal দেব।"
        )
    else:
        await send_join_message(update, context)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        store.upsert_user(update.effective_user, increment_photos=True)

    user_id = update.effective_user.id
    if store.is_user_blocked(user_id):
        await update.message.reply_text("আপনার access admin দ্বারা বন্ধ করা হয়েছে।")
        return

    if not await is_user_member(user_id, context):
        await send_join_message(update, context)
        return

    processing_msg = await update.message.reply_text("বিশ্লেষণ চলছে, একটু অপেক্ষা করুন...")
    photo_file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
        await photo_file.download_to_drive(tmp_file.name)
        tmp_path = tmp_file.name

    try:
        raw_analysis = analyze_image_with_gemini(tmp_path)
        if not raw_analysis:
            await processing_msg.edit_text("AI analysis failed হয়েছে। Admin panel থেকে API/config check করুন।")
            return

        parsed = parse_gemini_response(raw_analysis)
        final_text = format_final_message(parsed)
        await processing_msg.edit_text(final_text)
    except Exception as error:
        logger.error("Handler error: %s", error)
        await processing_msg.edit_text("একটি unexpected error হয়েছে। পরে আবার চেষ্টা করুন।")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        store.upsert_user(update.effective_user)

    if store.is_user_blocked(update.effective_user.id):
        await update.message.reply_text("আপনার access admin দ্বারা বন্ধ করা হয়েছে।")
        return

    await update.message.reply_text("শুধু chart image পাঠান। /start দিয়ে শুরু করুন।")


def main():
    Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(membership_check_callback, pattern="^check_membership$"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    logger.info("Bot is polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set. Add it as an environment variable.")
    main()
