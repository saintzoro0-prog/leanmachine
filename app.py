"""
Fitness Agent — Telegram + Gemini (free) — v3 "Jarvis edition"
----------------------------------------------------------------
Features:
- Butler-style persona in all messages (name + tone editable in settings)
- Menu (send "hi"/"menu"): Food Log | Workout | Log Weight | Profile | Demo
- Food Log -> Meal/Snack/Drink -> photo -> AI calories/macros; reply text to correct
- Profile: age, height, weight, sex, activity -> computes maintenance (TDEE) + deficit target
- Daily end-of-day report (/trigger/daily-report): totals vs target + flags foods that
  work against the abs goal
- Water: hourly reminder (/trigger/water-reminder) with personalized daily target
  (35 ml per kg bodyweight) and a tap-to-log +500ml button with progress
- Weekly report (/trigger/weekly-report), Monthly habits report (/trigger/monthly-report)
- Demo menu: fire any report instantly to yourself for testing + Reset My Data (with confirm)

Env vars: GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, FRIEND_CHAT_ID
"""

import os
import sqlite3
import base64
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

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
PERSONA_NAME = "Jarvis"
PERSONA_STYLE = (
    "You are a courteous, dry-witted British AI butler assisting with a fitness goal of defined abs. "
    "Address the user as 'sir'. Be concise, encouraging, lightly witty, never preachy. "
)

MENU_TEXT = "At your service, sir. What shall it be?"
MENU_BUTTONS = [
    {"text": "🍽 Food Log", "callback_data": "menu_food"},
    {"text": "💪 Workout", "callback_data": "menu_workout"},
]
MENU_BUTTONS_ROW2 = [
    {"text": "⚖️ Log Weight", "callback_data": "menu_weight"},
    {"text": "👤 Profile", "callback_data": "menu_profile"},
]
MENU_BUTTONS_ROW3 = [
    {"text": "💧 Log Water +500ml", "callback_data": "water_500"},
    {"text": "🧪 Demo", "callback_data": "menu_demo"},
]

FOOD_TYPE_TEXT = "Very good, sir. What are we logging?"
FOOD_TYPE_BUTTONS = [
    {"text": "Meal", "callback_data": "ft_meal"},
    {"text": "Snack", "callback_data": "ft_snack"},
    {"text": "Drink", "callback_data": "ft_drink"},
]

DAILY_PROMPT_TEXT = "Daily check-in, sir — how long did you train today?"
DURATION_OPTIONS = [
    {"text": "0 min", "callback_data": "dur_0"},
    {"text": "15-30 min", "callback_data": "dur_15-30"},
    {"text": "30-60 min", "callback_data": "dur_30-60"},
]

TARGET_PROMPT_TEXT = "And what did we target?"
TARGET_OPTIONS = [
    {"text": "Abs/Core", "callback_data": "tgt_Abs"},
    {"text": "Full body", "callback_data": "tgt_Full"},
    {"text": "Rest day", "callback_data": "tgt_Rest"},
]

SEX_BUTTONS = [
    {"text": "Male", "callback_data": "sex_m"},
    {"text": "Female", "callback_data": "sex_f"},
]
ACTIVITY_BUTTONS = [
    {"text": "Sedentary", "callback_data": "act_1.2"},
    {"text": "Light", "callback_data": "act_1.375"},
    {"text": "Moderate", "callback_data": "act_1.55"},
]
ACTIVITY_BUTTONS_ROW2 = [
    {"text": "Very active", "callback_data": "act_1.725"},
]

DEMO_TEXT = "Testing chamber, sir. Which system shall I fire?"
DEMO_BUTTONS = [
    {"text": "📋 Daily Report", "callback_data": "demo_daily"},
    {"text": "💧 Water Reminder", "callback_data": "demo_water"},
]
DEMO_BUTTONS_ROW2 = [
    {"text": "📊 Weekly Report", "callback_data": "demo_weekly"},
    {"text": "📅 Monthly Report", "callback_data": "demo_monthly"},
]
DEMO_BUTTONS_ROW3 = [
    {"text": "🗑 Reset My Data", "callback_data": "demo_reset"},
]

GREETING_WORDS = {"hi", "hello", "hey", "menu", "/start", "start", "yo", "jarvis"}

WATER_ML_PER_KG = 35          # daily water target: 35 ml per kg bodyweight
DEFAULT_WATER_TARGET_ML = 2500  # fallback if no weight logged
DEFICIT_KCAL = 500            # standard moderate deficit below maintenance
MIN_TARGET_KCAL = 1500        # never recommend eating below this

FOOD_ANALYSIS_PROMPT = (
    "You are a nutrition estimation tool. Identify the food(s) in this image and "
    "estimate total calories, carbs (g), protein (g), and fat (g) for the visible portion. "
    "Respond ONLY with valid JSON, no other text, no markdown fences, in this exact shape: "
    '{"food": "short description", "calories": number, "carbs": number, '
    '"protein": number, "fat": number, "confidence": "low|medium|high"}'
)


# ---------- DATABASE ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, food_type TEXT DEFAULT 'meal', food TEXT,
            calories REAL, carbs REAL, protein REAL, fat REAL, confidence TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, duration TEXT, target TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, weight_kg REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS water (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, ml INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            chat_id TEXT PRIMARY KEY,
            age INTEGER,
            height_cm REAL,
            start_weight_kg REAL,
            sex TEXT,
            activity REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS states (
            chat_id TEXT PRIMARY KEY,
            state TEXT,
            pending_food_type TEXT,
            last_meal_id INTEGER
        )
    """)
    conn.commit()
    conn.close()


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_start_iso():
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def get_state(chat_id):
    conn = db()
    row = conn.execute("SELECT * FROM states WHERE chat_id = ?", (str(chat_id),)).fetchone()
    conn.close()
    return dict(row) if row else {"chat_id": str(chat_id), "state": "", "pending_food_type": "", "last_meal_id": None}


def set_state(chat_id, state=None, pending_food_type=None, last_meal_id=None):
    cur = get_state(chat_id)
    if state is not None:
        cur["state"] = state
    if pending_food_type is not None:
        cur["pending_food_type"] = pending_food_type
    if last_meal_id is not None:
        cur["last_meal_id"] = last_meal_id
    conn = db()
    conn.execute(
        "INSERT INTO states (chat_id, state, pending_food_type, last_meal_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, "
        "pending_food_type=excluded.pending_food_type, last_meal_id=excluded.last_meal_id",
        (str(chat_id), cur["state"], cur["pending_food_type"], cur["last_meal_id"]),
    )
    conn.commit()
    conn.close()


# ---------- TELEGRAM ----------
def send_message(chat_id, text, buttons=None, button_rows=None):
    payload = {"chat_id": chat_id, "text": text}
    rows = None
    if button_rows:
        rows = [[{"text": b["text"], "callback_data": b["callback_data"]} for b in row] for row in button_rows]
    elif buttons:
        rows = [[{"text": b["text"], "callback_data": b["callback_data"]} for b in buttons]]
    if rows:
        payload["reply_markup"] = json.dumps({"inline_keyboard": rows})
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload)
    return r.json()


def answer_callback(callback_query_id):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": callback_query_id})


def download_telegram_photo(file_id: str):
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    content = requests.get(file_url).content
    return content, "image/jpeg"


# ---------- GEMINI ----------
def call_gemini(parts) -> str:
    payload = {"contents": [{"parts": parts}]}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        r = requests.post(
            GEMINI_API,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        result = r.json()
        if r.status_code == 503 and attempt < max_attempts:
            wait = attempt * 2
            print(f"GEMINI 503 (attempt {attempt}/{max_attempts}), retrying in {wait}s...", flush=True)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            print(f"GEMINI ERROR {r.status_code}: {result}", flush=True)
            return ""
        try:
            return result["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            print(f"GEMINI UNEXPECTED RESPONSE: {result}", flush=True)
            return ""
    return ""


def parse_food_json(raw_text: str) -> dict:
    fallback = {"food": "unrecognized", "calories": 0, "carbs": 0, "protein": 0, "fat": 0, "confidence": "low"}
    if not raw_text:
        return fallback
    raw_text = raw_text.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return fallback


def analyze_food_image(image_bytes: bytes, media_type: str) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    raw = call_gemini([
        {"text": FOOD_ANALYSIS_PROMPT},
        {"inline_data": {"mime_type": media_type, "data": image_b64}},
    ])
    return parse_food_json(raw)


def reanalyze_with_correction(original: dict, correction: str) -> dict:
    prompt = (
        "A nutrition estimate was made from a food photo, but the user has provided a correction. "
        f"Original estimate: {json.dumps(original)}. "
        f"User's correction: \"{correction}\". "
        "Produce a revised estimate incorporating the correction. "
        "Respond ONLY with valid JSON, no other text, no markdown fences, in this exact shape: "
        '{"food": "short description", "calories": number, "carbs": number, '
        '"protein": number, "fat": number, "confidence": "low|medium|high"}'
    )
    raw = call_gemini([{"text": prompt}])
    return parse_food_json(raw)


def format_food_reply(result: dict, food_type: str) -> str:
    return (
        f"Logged ({food_type}), sir: {result.get('food')}\n"
        f"Calories: {result.get('calories')} kcal\n"
        f"Carbs: {result.get('carbs')}g | Protein: {result.get('protein')}g | Fat: {result.get('fat')}g\n"
        f"Confidence: {result.get('confidence')}\n\n"
        f"If I've misjudged it, simply reply with a correction (e.g. \"they're chicken dumplings\")."
    )


# ---------- LOGGING ----------
def log_meal(chat_id, food_type, result) -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO meals (chat_id, timestamp, food_type, food, calories, carbs, protein, fat, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(chat_id), now_iso(), food_type,
         result.get("food", "unknown"), result.get("calories", 0), result.get("carbs", 0),
         result.get("protein", 0), result.get("fat", 0), result.get("confidence", "low")),
    )
    meal_id = cur.lastrowid
    conn.commit()
    conn.close()
    return meal_id


def update_meal(meal_id, result):
    conn = db()
    conn.execute(
        "UPDATE meals SET food=?, calories=?, carbs=?, protein=?, fat=?, confidence=? WHERE id=?",
        (result.get("food", "unknown"), result.get("calories", 0), result.get("carbs", 0),
         result.get("protein", 0), result.get("fat", 0), result.get("confidence", "low"), meal_id),
    )
    conn.commit()
    conn.close()


def log_workout(chat_id, duration, target):
    conn = db()
    conn.execute(
        "INSERT INTO workouts (chat_id, timestamp, duration, target) VALUES (?, ?, ?, ?)",
        (str(chat_id), now_iso(), duration, target),
    )
    conn.commit()
    conn.close()


def log_weight(chat_id, weight_kg):
    conn = db()
    conn.execute(
        "INSERT INTO weights (chat_id, timestamp, weight_kg) VALUES (?, ?, ?)",
        (str(chat_id), now_iso(), weight_kg),
    )
    conn.commit()
    conn.close()


def latest_weight(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT weight_kg FROM weights WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 1",
        (str(chat_id),),
    ).fetchone()
    conn.close()
    return row["weight_kg"] if row else None


def log_water(chat_id, ml):
    conn = db()
    conn.execute(
        "INSERT INTO water (chat_id, timestamp, ml) VALUES (?, ?, ?)",
        (str(chat_id), now_iso(), ml),
    )
    conn.commit()
    conn.close()


def water_today(chat_id) -> int:
    conn = db()
    row = conn.execute(
        "SELECT COALESCE(SUM(ml), 0) AS total FROM water WHERE chat_id = ? AND timestamp >= ?",
        (str(chat_id), today_start_iso()),
    ).fetchone()
    conn.close()
    return int(row["total"])


def get_profile(chat_id):
    conn = db()
    row = conn.execute("SELECT * FROM profiles WHERE chat_id = ?", (str(chat_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_profile(chat_id, **kwargs):
    existing = get_profile(chat_id) or {}
    merged = {
        "age": kwargs.get("age", existing.get("age")),
        "height_cm": kwargs.get("height_cm", existing.get("height_cm")),
        "start_weight_kg": kwargs.get("start_weight_kg", existing.get("start_weight_kg")),
        "sex": kwargs.get("sex", existing.get("sex")),
        "activity": kwargs.get("activity", existing.get("activity")),
    }
    conn = db()
    conn.execute(
        "INSERT INTO profiles (chat_id, age, height_cm, start_weight_kg, sex, activity) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET age=excluded.age, height_cm=excluded.height_cm, "
        "start_weight_kg=excluded.start_weight_kg, sex=excluded.sex, activity=excluded.activity",
        (str(chat_id), merged["age"], merged["height_cm"], merged["start_weight_kg"],
         merged["sex"], merged["activity"]),
    )
    conn.commit()
    conn.close()


# ---------- CALORIE TARGETS (Mifflin-St Jeor) ----------
def compute_targets(chat_id):
    """Returns (maintenance_kcal, deficit_target_kcal, water_target_ml) or (None, None, water) if profile incomplete."""
    profile = get_profile(chat_id)
    weight = latest_weight(chat_id) or (profile or {}).get("start_weight_kg")

    water_target = int(weight * WATER_ML_PER_KG) if weight else DEFAULT_WATER_TARGET_ML

    if not profile or not all([profile.get("age"), profile.get("height_cm"), weight,
                               profile.get("sex"), profile.get("activity")]):
        return None, None, water_target

    bmr = 10 * weight + 6.25 * profile["height_cm"] - 5 * profile["age"]
    bmr += 5 if profile["sex"] == "m" else -161
    maintenance = int(bmr * profile["activity"])
    deficit_target = max(maintenance - DEFICIT_KCAL, MIN_TARGET_KCAL)
    return maintenance, deficit_target, water_target


# ---------- REPORTS (all take a chat_id so demo can fire them at the tester) ----------
def send_daily_report(chat_id):
    conn = db()
    meals = conn.execute(
        "SELECT food_type, food, calories, carbs, protein, fat FROM meals "
        "WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
        (str(chat_id), today_start_iso()),
    ).fetchall()
    conn.close()

    maintenance, deficit_target, _ = compute_targets(chat_id)

    if not meals:
        send_message(chat_id, "Daily report, sir: nothing logged today. The abs remain a mystery to me as well.")
        return

    total_cal = sum(m["calories"] for m in meals)
    food_lines = "\n".join(
        f"- {m['food_type']}: {m['food']} ({m['calories']:.0f} kcal)" for m in meals
    )

    target_line = ""
    if deficit_target:
        status = "under" if total_cal <= deficit_target else "over"
        target_line = (
            f"Deficit target: {deficit_target} kcal (maintenance ≈ {maintenance} kcal). "
            f"You are {abs(total_cal - deficit_target):.0f} kcal {status} target.\n"
        )

    prompt = (
        f"{PERSONA_STYLE}"
        "Here is today's food log for someone whose goal is visible, defined abs:\n"
        f"{food_lines}\n"
        f"Total: {total_cal:.0f} kcal. {target_line}"
        "Write a short end-of-day report (max 120 words): first, flag which specific logged items "
        "work AGAINST the abs goal (calorie-dense, sugary, fried, alcohol) and briefly why; "
        "then note what was good; end with one supportive line for tomorrow. Plain text only."
    )
    review = call_gemini([{"text": prompt}])
    if not review:
        review = f"Total today: {total_cal:.0f} kcal.\n{target_line}(Analysis engine unavailable — try the demo again shortly.)"

    send_message(chat_id, f"📋 Daily Report\n\n{review}")


def send_water_reminder(chat_id):
    _, _, water_target = compute_targets(chat_id)
    drunk = water_today(chat_id)
    remaining = max(water_target - drunk, 0)
    if remaining == 0:
        text = f"💧 Hydration complete, sir — {drunk} ml down, target of {water_target} ml met. Carry on."
        send_message(chat_id, text)
        return
    text = (
        f"💧 Hydration check, sir. Progress: {drunk} / {water_target} ml today.\n"
        f"I'd suggest ~500 ml now — {remaining} ml to go."
    )
    send_message(chat_id, text, buttons=[{"text": "💧 +500 ml", "callback_data": "water_500"},
                                         {"text": "+250 ml", "callback_data": "water_250"}])


def send_weekly_report(chat_id):
    conn = db()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    meals = conn.execute(
        "SELECT calories, carbs, protein, fat FROM meals WHERE chat_id = ? AND timestamp >= ?",
        (str(chat_id), week_ago),
    ).fetchall()
    workouts = conn.execute(
        "SELECT duration, target FROM workouts WHERE chat_id = ? AND timestamp >= ? AND duration != ''",
        (str(chat_id), week_ago),
    ).fetchall()
    weights = conn.execute(
        "SELECT weight_kg, timestamp FROM weights WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
        (str(chat_id), week_ago),
    ).fetchall()
    conn.close()

    total_cal = sum(m["calories"] for m in meals)
    total_carbs = sum(m["carbs"] for m in meals)
    total_protein = sum(m["protein"] for m in meals)
    workout_days = len(workouts)

    weight_line = ""
    if len(weights) >= 2:
        change = weights[-1]["weight_kg"] - weights[0]["weight_kg"]
        weight_line = f"Weight change this week: {'+' if change >= 0 else ''}{change:.1f} kg\n"

    text = (
        "📊 Weekly Report, sir\n"
        f"Meals logged: {len(meals)}\n"
        f"Avg daily calories: {total_cal / 7:.0f} kcal\n"
        f"Avg daily carbs: {total_carbs / 7:.0f}g | Avg protein: {total_protein / 7:.0f}g\n"
        f"Workouts this week: {workout_days}/7\n"
        f"{weight_line}"
    )
    send_message(chat_id, text)


def send_monthly_report(chat_id):
    conn = db()
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    meals = conn.execute(
        "SELECT food_type, food, calories, carbs, protein, fat, timestamp FROM meals "
        "WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
        (str(chat_id), month_ago),
    ).fetchall()
    conn.close()

    if not meals:
        send_message(chat_id, "📅 Monthly report, sir: no food logged in the last 30 days. A clean slate, as it were.")
        return

    lines = [
        f"{m['timestamp'][:10]} | {m['food_type']} | {m['food']} | {m['calories']} kcal | "
        f"{m['carbs']}g carbs | {m['protein']}g protein | {m['fat']}g fat"
        for m in meals
    ]
    data_block = "\n".join(lines)

    prompt = (
        f"{PERSONA_STYLE}"
        "Here is 30 days of the user's food log (one line per item):\n\n"
        f"{data_block}\n\n"
        "Write a short monthly eating-habits summary (max 150 words) covering: "
        "average daily calories, the meal/snack/drink balance, most frequently eaten foods, "
        "notable patterns (high-carb days, low-protein stretches, frequent snacking), "
        "and one or two practical, supportive suggestions. Plain text only, no markdown."
    )
    summary = call_gemini([{"text": prompt}])
    if not summary:
        summary = "The analysis engine is momentarily indisposed, sir. Do try again shortly."
    send_message(chat_id, f"📅 Monthly Eating Habits\n\n{summary}")


def reset_user_data(chat_id):
    conn = db()
    for table in ("meals", "workouts", "weights", "water", "profiles", "states"):
        conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (str(chat_id),))
    conn.commit()
    conn.close()


# ---------- MENU ----------
def show_menu(chat_id):
    send_message(chat_id, MENU_TEXT, button_rows=[MENU_BUTTONS, MENU_BUTTONS_ROW2, MENU_BUTTONS_ROW3])


# ---------- WEBHOOK ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    print(f"INCOMING UPDATE: {update}", flush=True)

    # ----- Photo -----
    if "message" in update and "photo" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        try:
            state = get_state(chat_id)
            food_type = state.get("pending_food_type") or "meal"
            file_id = update["message"]["photo"][-1]["file_id"]
            image_bytes, mime_type = download_telegram_photo(file_id)
            result = analyze_food_image(image_bytes, mime_type)
            print(f"Gemini result: {result}", flush=True)
            meal_id = log_meal(chat_id, food_type, result)
            set_state(chat_id, state="can_correct", pending_food_type="", last_meal_id=meal_id)
            send_message(chat_id, format_food_reply(result, food_type))
        except Exception as e:
            print(f"PHOTO HANDLING FAILED: {type(e).__name__}: {e}", flush=True)
            send_message(chat_id, f"My apologies, sir — something went wrong analyzing that photo: {e}")
        return jsonify(ok=True)

    # ----- Text -----
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"].strip()
        state = get_state(chat_id)

        if text.lower() in GREETING_WORDS:
            set_state(chat_id, state="", pending_food_type="")
            show_menu(chat_id)
            return jsonify(ok=True)

        if state.get("state") == "awaiting_weight":
            try:
                weight = float(text.replace(",", "."))
                log_weight(chat_id, weight)
                profile = get_profile(chat_id)
                if profile and profile.get("start_weight_kg"):
                    diff = weight - profile["start_weight_kg"]
                    trend = f" ({'+' if diff >= 0 else ''}{diff:.1f} kg since start)"
                else:
                    trend = ""
                set_state(chat_id, state="")
                send_message(chat_id, f"Weight logged, sir: {weight:.1f} kg{trend} ✅")
            except ValueError:
                send_message(chat_id, "Just the number please, sir — e.g. 72.5")
            return jsonify(ok=True)

        if state.get("state") == "awaiting_age":
            try:
                age = int(text)
                save_profile(chat_id, age=age)
                set_state(chat_id, state="awaiting_height")
                send_message(chat_id, "Noted. And your height in cm, sir?")
            except ValueError:
                send_message(chat_id, "Your age as a whole number please, sir — e.g. 21")
            return jsonify(ok=True)

        if state.get("state") == "awaiting_height":
            try:
                height = float(text.replace(",", "."))
                save_profile(chat_id, height_cm=height)
                set_state(chat_id, state="awaiting_start_weight")
                send_message(chat_id, "Very good. Current weight in kg? (Just the number.)")
            except ValueError:
                send_message(chat_id, "Height in cm please, sir — e.g. 175")
            return jsonify(ok=True)

        if state.get("state") == "awaiting_start_weight":
            try:
                weight = float(text.replace(",", "."))
                save_profile(chat_id, start_weight_kg=weight)
                log_weight(chat_id, weight)
                set_state(chat_id, state="")
                send_message(chat_id, "And your biological sex? (Needed for the calorie mathematics.)",
                             buttons=SEX_BUTTONS)
            except ValueError:
                send_message(chat_id, "Just the number please, sir — e.g. 72.5")
            return jsonify(ok=True)

        if state.get("state") == "can_correct" and state.get("last_meal_id"):
            conn = db()
            row = conn.execute("SELECT * FROM meals WHERE id = ?", (state["last_meal_id"],)).fetchone()
            conn.close()
            if row:
                original = {"food": row["food"], "calories": row["calories"], "carbs": row["carbs"],
                            "protein": row["protein"], "fat": row["fat"], "confidence": row["confidence"]}
                revised = reanalyze_with_correction(original, text)
                update_meal(state["last_meal_id"], revised)
                send_message(chat_id, "Amended, sir ✅\n" + format_food_reply(revised, row["food_type"]))
            else:
                send_message(chat_id, "I couldn't locate the last entry to amend, sir. Send 'menu' to start over.")
            return jsonify(ok=True)

        send_message(chat_id, "Send 'menu' for options, sir, or a food photo to log it.")
        return jsonify(ok=True)

    # ----- Button taps -----
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq["data"]
        answer_callback(cq["id"])

        if data == "menu_food":
            send_message(chat_id, FOOD_TYPE_TEXT, buttons=FOOD_TYPE_BUTTONS)
        elif data == "menu_workout":
            send_message(chat_id, DAILY_PROMPT_TEXT, buttons=DURATION_OPTIONS)
        elif data == "menu_weight":
            set_state(chat_id, state="awaiting_weight")
            send_message(chat_id, "Your weight in kg, sir (just the number, e.g. 72.5).")
        elif data == "menu_profile":
            set_state(chat_id, state="awaiting_age")
            send_message(chat_id, "Profile setup, sir. How old are you?")
        elif data == "menu_demo":
            send_message(chat_id, DEMO_TEXT, button_rows=[DEMO_BUTTONS, DEMO_BUTTONS_ROW2, DEMO_BUTTONS_ROW3])
        elif data.startswith("ft_"):
            food_type = data.replace("ft_", "")
            set_state(chat_id, state="", pending_food_type=food_type)
            send_message(chat_id, f"Send a photo of your {food_type}, sir 📸")
        elif data.startswith("dur_"):
            duration = data.replace("dur_", "")
            log_workout(chat_id, duration, "")
            send_message(chat_id, TARGET_PROMPT_TEXT, buttons=TARGET_OPTIONS)
        elif data.startswith("tgt_"):
            target = data.replace("tgt_", "")
            log_workout(chat_id, "", target)
            send_message(chat_id, f"Logged: {target}. Well done, sir.")
        elif data.startswith("sex_"):
            save_profile(chat_id, sex=data.replace("sex_", ""))
            send_message(chat_id, "And how active are you day to day, sir?",
                         button_rows=[ACTIVITY_BUTTONS, ACTIVITY_BUTTONS_ROW2])
        elif data.startswith("act_"):
            save_profile(chat_id, activity=float(data.replace("act_", "")))
            maintenance, deficit_target, water_target = compute_targets(chat_id)
            send_message(
                chat_id,
                f"Profile complete, sir ✅\n\n"
                f"Estimated maintenance: ~{maintenance} kcal/day\n"
                f"Recommended target for the abs project: ~{deficit_target} kcal/day "
                f"(a moderate {DEFICIT_KCAL} kcal deficit — sustainable beats drastic)\n"
                f"Daily water target: ~{water_target} ml\n\n"
                f"These are estimates, sir — we'll refine as the weigh-ins come in.",
            )
        elif data == "water_500":
            log_water(chat_id, 500)
            _, _, water_target = compute_targets(chat_id)
            send_message(chat_id, f"💧 Logged 500 ml. {water_today(chat_id)} / {water_target} ml today.")
        elif data == "water_250":
            log_water(chat_id, 250)
            _, _, water_target = compute_targets(chat_id)
            send_message(chat_id, f"💧 Logged 250 ml. {water_today(chat_id)} / {water_target} ml today.")
        elif data == "demo_daily":
            send_daily_report(chat_id)
        elif data == "demo_water":
            send_water_reminder(chat_id)
        elif data == "demo_weekly":
            send_weekly_report(chat_id)
        elif data == "demo_monthly":
            send_monthly_report(chat_id)
        elif data == "demo_reset":
            send_message(chat_id, "This erases ALL your logged data, sir — meals, workouts, weights, water, profile. "
                                  "Quite irreversible. Certain?",
                         buttons=[{"text": "Yes, wipe it", "callback_data": "demo_reset_confirm"},
                                  {"text": "Cancel", "callback_data": "demo_reset_cancel"}])
        elif data == "demo_reset_confirm":
            reset_user_data(chat_id)
            send_message(chat_id, "Done, sir. A blank slate. Send 'menu' to begin anew.")
        elif data == "demo_reset_cancel":
            send_message(chat_id, "Wise choice, sir. Data intact.")
        return jsonify(ok=True)

    return jsonify(ok=True)


# ---------- TRIGGERS (cron-job.org) ----------
@app.route("/trigger/daily-prompt", methods=["GET", "POST"])
def trigger_daily_prompt():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_message(FRIEND_CHAT_ID, DAILY_PROMPT_TEXT, buttons=DURATION_OPTIONS)
    return jsonify(status="sent"), 200


@app.route("/trigger/daily-report", methods=["GET", "POST"])
def trigger_daily_report():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_daily_report(FRIEND_CHAT_ID)
    return jsonify(status="sent"), 200


@app.route("/trigger/water-reminder", methods=["GET", "POST"])
def trigger_water_reminder():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_water_reminder(FRIEND_CHAT_ID)
    return jsonify(status="sent"), 200


@app.route("/trigger/weight-reminder", methods=["GET", "POST"])
def trigger_weight_reminder():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    set_state(FRIEND_CHAT_ID, state="awaiting_weight")
    send_message(FRIEND_CHAT_ID,
                 "⚖️ Morning weigh-in, sir. For consistency: after waking, after the bathroom, "
                 "before food or drink.\n\nReply with your weight in kg (just the number).")
    return jsonify(status="sent"), 200


@app.route("/trigger/weekly-report", methods=["GET", "POST"])
def trigger_weekly_report():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_weekly_report(FRIEND_CHAT_ID)
    return jsonify(status="sent"), 200


@app.route("/trigger/monthly-report", methods=["GET", "POST"])
def trigger_monthly_report():
    if not FRIEND_CHAT_ID:
        return jsonify(status="no chat id set"), 400
    send_monthly_report(FRIEND_CHAT_ID)
    return jsonify(status="sent"), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify(status="alive"), 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    render_url = request.args.get("url")
    if not render_url:
        return jsonify(error="pass ?url=https://your-app.onrender.com"), 400
    r = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": f"{render_url}/webhook"})
    return jsonify(r.json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
