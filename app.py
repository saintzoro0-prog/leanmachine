"""
Fitness Agent — Telegram + Gemini version (100% free, no card, no trial)
--------------------------------------------------------------------------
Uses Telegram Bot API (free) + Google Gemini API (free tier, no credit card,
no expiry — 1,500 requests/day) + SQLite. No Meta/Facebook account, no
Anthropic billing.

SETUP (takes ~5 minutes):

1. Open Telegram (app or web.telegram.org).
2. Search for the user "@BotFather" and open a chat with it.
3. Send: /newbot
4. Give it a name (e.g. "Abs Tracker") and a username ending in "bot" (e.g. "abstracker_bot").
5. BotFather replies with an API token that looks like: 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   -> This is your TELEGRAM_BOT_TOKEN.
6. Your friend opens Telegram, searches for the bot's username, and sends it any message
   (e.g. "hi") once. This is required so the bot knows their chat ID.
7. Gemini API key (free, no card): aistudio.google.com -> sign in with Google -> Get API Key -> Create API key.
8. Free hosting: render.com -> New Web Service -> connect a GitHub repo with this code.
9. Free external cron: cron-job.org -> schedule pings to the trigger URLs (see SETUP.md).

Env vars needed:
   GEMINI_API_KEY
   TELEGRAM_BOT_TOKEN
   FRIEND_CHAT_ID     (see SETUP.md for how to get this after step 6)
"""

import os
import sqlite3
import base64
import json
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()  # reads .env file in this folder, if present

# ---------- CONFIG ----------
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
FRIEND_CHAT_ID = os.environ.get("FRIEND_CHAT_ID", "")

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_API = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DB_PATH = os.path.join(os.path.dirname(__file__), "fitness.db")

app = Flask(__name__)


# ---------- EDITABLE SETTINGS ----------
# Change these directly for the most common tweaks — no need to touch logic below.
# For anything not covered here, describe the change you want and it can be edited for you.

DAILY_PROMPT_TEXT = "Daily check-in — how long did you work out today?"
DURATION_OPTIONS = [
    {"text": "0 min", "callback_data": "dur_0"},
    {"text": "15-30 min", "callback_data": "dur_15-30"},
    {"text": "30-60 min", "callback_data": "dur_30-60"},
]

TARGET_PROMPT_TEXT = "What did you target?"
TARGET_OPTIONS = [
    {"text": "Abs/Core", "callback_data": "tgt_Abs"},
    {"text": "Full body", "callback_data": "tgt_Full"},
    {"text": "Rest day", "callback_data": "tgt_Rest"},
]

FOOD_ANALYSIS_PROMPT = (
    "You are a nutrition estimation tool. Identify the food(s) in this image and "
    "estimate total calories, carbs (g), protein (g), and fat (g) for the visible portion. "
    "Respond ONLY with valid JSON, no other text, no markdown fences, in this exact shape: "
    '{"food": "short description", "calories": number, "carbs": number, '
    '"protein": number, "fat": number, "confidence": "low|medium|high"}'
)

WEEKLY_REPORT_HEADER = "Weekly Report"


# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, food TEXT,
            calories REAL, carbs REAL, protein REAL, fat REAL, confidence TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, duration TEXT, target TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def log_meal(chat_id, food, calories, carbs, protein, fat, confidence):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO meals (chat_id, timestamp, food, calories, carbs, protein, fat, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(chat_id), datetime.now(timezone.utc).isoformat(), food, calories, carbs, protein, fat, confidence),
    )
    conn.commit()
    conn.close()


def log_workout(chat_id, duration, target):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO workouts (chat_id, timestamp, duration, target) VALUES (?, ?, ?, ?)",
        (str(chat_id), datetime.now(timezone.utc).isoformat(), duration, target),
    )
    conn.commit()
    conn.close()


# ---------- TELEGRAM SEND ----------
def send_message(chat_id, text, buttons=None):
    """buttons: list of {'text': 'label', 'callback_data': 'value'} shown as one row of inline buttons"""
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": b["text"], "callback_data": b["callback_data"]} for b in buttons]]
        })
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload)
    return r.json()


def answer_callback(callback_query_id):
    """Stops the button's loading spinner on the user's phone."""
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": callback_query_id})


def download_telegram_photo(file_id: str):
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    content = requests.get(file_url).content
    return content, "image/jpeg"


# ---------- GEMINI VISION: FOOD ANALYSIS (free, no card) ----------
def analyze_food_image(image_bytes: bytes, media_type: str) -> dict:
    import time

    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [{
            "parts": [
                {"text": FOOD_ANALYSIS_PROMPT},
                {"inline_data": {"mime_type": media_type, "data": image_b64}},
            ]
        }]
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            GEMINI_API,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        result = r.json()

        if r.status_code == 503 and attempt < max_attempts:
            wait = attempt * 2  # 2s, then 4s
            print(f"GEMINI 503 (attempt {attempt}/{max_attempts}), retrying in {wait}s...", flush=True)
            time.sleep(wait)
            continue

        if r.status_code != 200:
            print(f"GEMINI ERROR {r.status_code}: {result}", flush=True)
            return {"food": f"API error {r.status_code}", "calories": 0, "carbs": 0, "protein": 0, "fat": 0, "confidence": "low"}

        try:
            raw_text = result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            print(f"GEMINI UNEXPECTED RESPONSE: {result}", flush=True)
            return {"food": "unrecognized", "calories": 0, "carbs": 0, "protein": 0, "fat": 0, "confidence": "low"}

        raw_text = raw_text.strip().removeprefix("```json").removesuffix("```").strip()
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return {"food": "unrecognized", "calories": 0, "carbs": 0, "protein": 0, "fat": 0, "confidence": "low"}

    return {"food": "Gemini busy, try again", "calories": 0, "carbs": 0, "protein": 0, "fat": 0, "confidence": "low"}


# ---------- WEBHOOK: INCOMING TELEGRAM UPDATES ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    print(f"INCOMING UPDATE: {update}", flush=True)

    # Case 1: photo message
    if "message" in update and "photo" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        try:
            # Telegram sends multiple resolutions; the last one is highest quality
            file_id = update["message"]["photo"][-1]["file_id"]
            print(f"Downloading photo file_id={file_id}", flush=True)
            image_bytes, mime_type = download_telegram_photo(file_id)
            print(f"Downloaded {len(image_bytes)} bytes, calling Gemini...", flush=True)
            result = analyze_food_image(image_bytes, mime_type)
            print(f"Gemini result: {result}", flush=True)

            log_meal(
                chat_id,
                result.get("food", "unknown"),
                result.get("calories", 0),
                result.get("carbs", 0),
                result.get("protein", 0),
                result.get("fat", 0),
                result.get("confidence", "low"),
            )
            send_message(
                chat_id,
                f"Logged: {result.get('food')}\n"
                f"Calories: {result.get('calories')} kcal\n"
                f"Carbs: {result.get('carbs')}g | Protein: {result.get('protein')}g | Fat: {result.get('fat')}g\n"
                f"Confidence: {result.get('confidence')}",
            )
        except Exception as e:
            print(f"PHOTO HANDLING FAILED: {type(e).__name__}: {e}", flush=True)
            send_message(chat_id, f"Something went wrong analyzing that photo: {e}")
        return jsonify(ok=True)

    # Case 2: plain text (e.g. first "hi" message, or anything not a photo/button)
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        send_message(chat_id, f"Your chat ID is {chat_id}. Send a food photo to log a meal.")
        return jsonify(ok=True)

    # Case 3: button tap (callback_query)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq["data"]
        answer_callback(cq["id"])

        if data.startswith("dur_"):
            duration = data.replace("dur_", "")
            log_workout(chat_id, duration, "")
            send_message(chat_id, TARGET_PROMPT_TEXT, buttons=TARGET_OPTIONS)
        elif data.startswith("tgt_"):
            target = data.replace("tgt_", "")
            log_workout(chat_id, "", target)
            send_message(chat_id, f"Logged target: {target}. Nice work.")
        return jsonify(ok=True)

    return jsonify(ok=True)


# ---------- TRIGGER ENDPOINTS (hit these from a free external cron, e.g. cron-job.org) ----------
@app.route("/trigger/daily-prompt", methods=["GET", "POST"])
def trigger_daily_prompt():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_message(FRIEND_CHAT_ID, DAILY_PROMPT_TEXT, buttons=DURATION_OPTIONS)
    return jsonify(status="sent"), 200


@app.route("/trigger/weekly-report", methods=["GET", "POST"])
def trigger_weekly_report():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    conn = sqlite3.connect(DB_PATH)
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    meals = conn.execute(
        "SELECT calories, carbs, protein, fat FROM meals WHERE chat_id = ? AND timestamp >= ?",
        (str(FRIEND_CHAT_ID), week_ago),
    ).fetchall()
    workouts = conn.execute(
        "SELECT duration, target FROM workouts WHERE chat_id = ? AND timestamp >= ?",
        (str(FRIEND_CHAT_ID), week_ago),
    ).fetchall()
    conn.close()

    total_cal = sum(m[0] for m in meals)
    total_carbs = sum(m[1] for m in meals)
    total_protein = sum(m[2] for m in meals)
    workout_days = len(workouts)

    text = (
        f"{WEEKLY_REPORT_HEADER}\n"
        f"Meals logged: {len(meals)}\n"
        f"Avg daily calories: {total_cal / 7:.0f} kcal\n"
        f"Avg daily carbs: {total_carbs / 7:.0f}g | Avg protein: {total_protein / 7:.0f}g\n"
        f"Workouts this week: {workout_days}/7\n"
    )
    send_message(FRIEND_CHAT_ID, text)
    return jsonify(status="sent"), 200


@app.route("/", methods=["GET"])
def health():
    """Free external cron also pings this every ~10 min to stop the free host from sleeping."""
    return jsonify(status="alive"), 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    """Visit this once (in a browser) after deploying, to tell Telegram where to send updates."""
    render_url = request.args.get("url")  # e.g. ?url=https://your-app.onrender.com
    if not render_url:
        return jsonify(error="pass ?url=https://your-app.onrender.com"), 400
    r = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": f"{render_url}/webhook"})
    return jsonify(r.json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
