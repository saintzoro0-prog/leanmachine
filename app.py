"""
Fitness Agent — Telegram + Gemini — v4 "Persistent edition"
--------------------------------------------------------------
Data now lives in Turso (free hosted database) so NOTHING is lost when the
app redeploys, restarts, or sleeps. Update app.py as often as you like —
profiles, meals, weights, water all survive.

If TURSO_* env vars are not set, falls back to local SQLite (dev mode only —
data will NOT survive redeploys on Render without Turso).

New in v4:
- Turso persistent storage
- Correct "today" boundaries for your timezone (default UTC+4 Dubai) —
  a meal at 11 PM now counts for the right day
- "undo" / "/undo" command deletes the last logged food item

Env vars:
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, FRIEND_CHAT_ID (comma-separated)
  TURSO_DATABASE_URL, TURSO_AUTH_TOKEN   <- the persistence fix
  ADMIN_CHAT_ID (your /myid, unlocks /admin)
  SHORTCUT_TOKEN (optional, locks /shortcut/* endpoints)
  TZ_OFFSET_HOURS (optional, default 4 = Dubai)
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
FRIEND_CHAT_IDS = [c.strip() for c in os.environ.get("FRIEND_CHAT_ID", "").split(",") if c.strip()]
SHORTCUT_TOKEN = os.environ.get("SHORTCUT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
TZ_OFFSET_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "4"))  # Dubai = UTC+4

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_API = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
DB_PATH = os.path.join(os.path.dirname(__file__), "fitness.db")

app = Flask(__name__)

# ---------- DATABASE LAYER (Turso via HTTP API if configured, local SQLite otherwise) ----------
# NOTE: we use Turso's plain HTTP "pipeline" API via `requests` instead of the libsql_client
# package — that package is unmaintained (archived) and its websocket transport was failing.
TURSO_HTTP_URL = TURSO_DATABASE_URL.replace("libsql://", "https://") if TURSO_DATABASE_URL else ""

if USE_TURSO:
    print("DB: connected to Turso via HTTP (persistent)", flush=True)
else:
    print("DB: local SQLite (EPHEMERAL on Render — set TURSO_* env vars for persistence)", flush=True)


def _turso_execute(sql, params=()):
    """Runs one SQL statement against Turso's HTTP pipeline API. Returns the raw result dict."""
    # Turso's HTTP API wants typed args: {"type": "text"/"integer"/"float"/"null", "value": ...}
    def wrap(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": "1" if v else "0"}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    body = {"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": [wrap(p) for p in params]}},
        {"type": "close"},
    ]}
    r = requests.post(
        f"{TURSO_HTTP_URL}/v2/pipeline",
        headers={"Authorization": f"Bearer {TURSO_AUTH_TOKEN}", "Content-Type": "application/json"},
        json=body,
        timeout=15,
    )
    if r.status_code != 200:
        print(f"TURSO HTTP ERROR {r.status_code}: {r.text}", flush=True)
        raise RuntimeError(f"Turso error {r.status_code}: {r.text}")
    data = r.json()
    result_entry = data["results"][0]
    if result_entry["type"] == "error":
        print(f"TURSO SQL ERROR: {result_entry}", flush=True)
        raise RuntimeError(f"Turso SQL error: {result_entry}")
    return result_entry["response"]["result"]


def q(sql, params=()):
    """Run a read query, return list of dicts."""
    if USE_TURSO:
        result = _turso_execute(sql, params)
        cols = [c["name"] for c in result["cols"]]
        rows = []
        for raw_row in result["rows"]:
            row = {}
            for col, cell in zip(cols, raw_row):
                v = cell.get("value")
                if cell.get("type") == "integer" and v is not None:
                    v = int(v)
                elif cell.get("type") == "float" and v is not None:
                    v = float(v)
                row[col] = v
            rows.append(row)
        return rows
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def x(sql, params=()):
    """Run a write query, return last inserted row id."""
    if USE_TURSO:
        result = _turso_execute(sql, params)
        return result.get("last_insert_rowid")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(sql, params)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def init_db():
    ddl = [
        """CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, food_type TEXT DEFAULT 'meal', food TEXT,
            calories REAL, carbs REAL, protein REAL, fat REAL, confidence TEXT)""",
        """CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, duration TEXT, target TEXT, intensity TEXT)""",
        """CREATE TABLE IF NOT EXISTS weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, weight_kg REAL)""",
        """CREATE TABLE IF NOT EXISTS water (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, ml INTEGER)""",
        """CREATE TABLE IF NOT EXISTS profiles (
            chat_id TEXT PRIMARY KEY, name TEXT, age INTEGER, height_cm REAL,
            start_weight_kg REAL, sex TEXT, activity REAL)""",
        """CREATE TABLE IF NOT EXISTS states (
            chat_id TEXT PRIMARY KEY, state TEXT, pending_food_type TEXT,
            last_meal_id INTEGER, pending_duration TEXT, pending_target TEXT)""",
        """CREATE TABLE IF NOT EXISTS progress_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT, timestamp TEXT, file_id TEXT)""",
    ]
    for stmt in ddl:
        x(stmt)


init_db()


# ---------- TIME (timezone-aware "today") ----------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def today_start_iso():
    """Start of *local* day (per TZ_OFFSET_HOURS), expressed in UTC ISO for comparisons."""
    local_now = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_equivalent = local_midnight - timedelta(hours=TZ_OFFSET_HOURS)
    return utc_equivalent.isoformat()


# ---------- EDITABLE SETTINGS ----------
PERSONA_NAME = "Jarvis"
PERSONA_STYLE = (
    "You are a courteous, dry-witted British AI butler assisting with a fitness goal of defined abs. "
    "Address the user as 'sir'. Be concise, encouraging, lightly witty, never preachy. "
    "Ground every claim in concrete numbers and real physiology — actual kcal figures, protein in grams "
    "(target 1.6–2.2 g per kg bodyweight for muscle retention in a deficit), fibre and satiety, energy density, "
    "sensible fat-loss pace (~0.5% of bodyweight per week). Simple words, real science, no vague filler like "
    "'eat healthy' or 'stay balanced'. "
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
    {"text": "🍳 Hungry", "callback_data": "menu_hungry"},
    {"text": "🧠 Body Insights", "callback_data": "menu_insights"},
]
MENU_BUTTONS_ROW4 = [
    {"text": "💧 Log Water +500ml", "callback_data": "water_500"},
    {"text": "🧪 Demo", "callback_data": "menu_demo"},
]

FOOD_TYPE_TEXT = "Very good, sir. What are we logging?"
FOOD_TYPE_BUTTONS = [
    {"text": "🍽 Meal", "callback_data": "ft_meal"},
    {"text": "🍿 Snack", "callback_data": "ft_snack"},
    {"text": "🥤 Drink", "callback_data": "ft_drink"},
]

DAILY_PROMPT_TEXT = "💪 Daily check-in — how long did you train today?"
DURATION_OPTIONS = [
    {"text": "🚫 0 min", "callback_data": "dur_0"},
    {"text": "⏱ 15-30", "callback_data": "dur_15-30"},
    {"text": "⏱ 30-60", "callback_data": "dur_30-60"},
]
DURATION_OPTIONS_ROW2 = [
    {"text": "⏱ 60-90", "callback_data": "dur_60-90"},
    {"text": "🔥 90+", "callback_data": "dur_90+"},
]

TARGET_PROMPT_TEXT = "🎯 And what did we target?"
TARGET_OPTIONS = [
    {"text": "🔥 Abs/Core", "callback_data": "tgt_Abs"},
    {"text": "🫁 Chest", "callback_data": "tgt_Chest"},
    {"text": "🔙 Back", "callback_data": "tgt_Back"},
]
TARGET_OPTIONS_ROW2 = [
    {"text": "🦵 Legs", "callback_data": "tgt_Legs"},
    {"text": "💪 Arms", "callback_data": "tgt_Arms"},
    {"text": "🏃 Cardio", "callback_data": "tgt_Cardio"},
]
TARGET_OPTIONS_ROW3 = [
    {"text": "🏋️ Full body", "callback_data": "tgt_Full body"},
    {"text": "😴 Rest day", "callback_data": "tgt_Rest day"},
]

INTENSITY_PROMPT_TEXT = "⚡ How hard did it feel?"
INTENSITY_OPTIONS = [
    {"text": "😌 Easy", "callback_data": "int_Easy"},
    {"text": "😤 Moderate", "callback_data": "int_Moderate"},
    {"text": "🥵 Hard", "callback_data": "int_Hard"},
]

REPLY_KEYBOARD = [
    ["🍽 Log Food", "💪 Workout"],
    ["💧 Water +500", "⚖️ Weight"],
    ["🍳 Hungry", "📋 Menu"],
]

GREETING_WORDS = {"hi", "hello", "hey", "menu", "/start", "start", "yo", "jarvis"}

WATER_ML_PER_KG = 35
DEFAULT_WATER_TARGET_ML = 2500
DEFICIT_KCAL = 500
MIN_TARGET_KCAL = 1500

FOOD_ANALYSIS_PROMPT = (
    "You are a nutrition estimation tool. Identify the food(s) in this image and "
    "estimate total calories, carbs (g), protein (g), and fat (g) for the visible portion. "
    "Respond ONLY with valid JSON, no other text, no markdown fences, in this exact shape: "
    '{"food": "short description", "calories": number, "carbs": number, '
    '"protein": number, "fat": number, "confidence": "low|medium|high"}'
)

VOICE_EXTRACT_PROMPT = (
    "Listen to this voice note. The speaker is reporting either food they ate, a workout they did, or both. "
    "Extract the information and respond ONLY with valid JSON, no other text, no markdown fences:\n"
    '{"items": [\n'
    '  {"kind": "food", "food_type": "meal|snack|drink", "food": "description", '
    '"calories": number, "carbs": number, "protein": number, "fat": number, "confidence": "low|medium|high"},\n'
    '  {"kind": "workout", "duration": "minutes as string", "target": "Abs|Chest|Back|Legs|Arms|Cardio|Full body|Rest day"}\n'
    "]}\n"
    "Include one entry per distinct thing mentioned. Estimate nutrition from the description. "
    'If the audio contains neither food nor workout info, return {"items": []}.'
)


# ---------- STATE ----------
def get_state(chat_id):
    rows = q("SELECT * FROM states WHERE chat_id = ?", (str(chat_id),))
    if rows:
        return rows[0]
    return {"chat_id": str(chat_id), "state": "", "pending_food_type": "",
            "last_meal_id": None, "pending_duration": "", "pending_target": ""}


def set_state(chat_id, state=None, pending_food_type=None, last_meal_id=None,
              pending_duration=None, pending_target=None):
    cur = get_state(chat_id)
    if state is not None:
        cur["state"] = state
    if pending_food_type is not None:
        cur["pending_food_type"] = pending_food_type
    if last_meal_id is not None:
        cur["last_meal_id"] = last_meal_id
    if pending_duration is not None:
        cur["pending_duration"] = pending_duration
    if pending_target is not None:
        cur["pending_target"] = pending_target
    x("INSERT INTO states (chat_id, state, pending_food_type, last_meal_id, pending_duration, pending_target) "
      "VALUES (?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(chat_id) DO UPDATE SET state=excluded.state, "
      "pending_food_type=excluded.pending_food_type, last_meal_id=excluded.last_meal_id, "
      "pending_duration=excluded.pending_duration, pending_target=excluded.pending_target",
      (str(chat_id), cur["state"], cur["pending_food_type"], cur["last_meal_id"],
       cur.get("pending_duration", ""), cur.get("pending_target", "")))


# ---------- TELEGRAM ----------
def send_message(chat_id, text, buttons=None, button_rows=None, with_keyboard=False):
    payload = {"chat_id": chat_id, "text": text}
    rows = None
    if button_rows:
        rows = [[{"text": b["text"], "callback_data": b["callback_data"]} for b in row] for row in button_rows]
    elif buttons:
        rows = [[{"text": b["text"], "callback_data": b["callback_data"]} for b in buttons]]
    if rows:
        payload["reply_markup"] = json.dumps({"inline_keyboard": rows})
    elif with_keyboard:
        payload["reply_markup"] = json.dumps({
            "keyboard": [[{"text": t} for t in row] for row in REPLY_KEYBOARD],
            "resize_keyboard": True,
            "is_persistent": True,
        })
    r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload)
    return r.json()


def answer_callback(callback_query_id):
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", data={"callback_query_id": callback_query_id})


def download_telegram_file(file_id: str):
    file_info = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    content = requests.get(file_url).content
    return content


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
        f"If I've misjudged it, reply with a correction — or send 'undo' to remove it."
    )


# ---------- LOGGING ----------
def log_meal(chat_id, food_type, result) -> int:
    return x("INSERT INTO meals (chat_id, timestamp, food_type, food, calories, carbs, protein, fat, confidence) "
             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (str(chat_id), now_iso(), food_type,
              result.get("food", "unknown"), result.get("calories", 0), result.get("carbs", 0),
              result.get("protein", 0), result.get("fat", 0), result.get("confidence", "low")))


def update_meal(meal_id, result):
    x("UPDATE meals SET food=?, calories=?, carbs=?, protein=?, fat=?, confidence=? WHERE id=?",
      (result.get("food", "unknown"), result.get("calories", 0), result.get("carbs", 0),
       result.get("protein", 0), result.get("fat", 0), result.get("confidence", "low"), meal_id))


def delete_last_meal(chat_id):
    rows = q("SELECT id, food FROM meals WHERE chat_id = ? ORDER BY id DESC LIMIT 1", (str(chat_id),))
    if not rows:
        return None
    x("DELETE FROM meals WHERE id = ?", (rows[0]["id"],))
    return rows[0]["food"]


def log_workout(chat_id, duration, target, intensity=""):
    x("INSERT INTO workouts (chat_id, timestamp, duration, target, intensity) VALUES (?, ?, ?, ?, ?)",
      (str(chat_id), now_iso(), duration, target, intensity))


def log_weight(chat_id, weight_kg):
    x("INSERT INTO weights (chat_id, timestamp, weight_kg) VALUES (?, ?, ?)",
      (str(chat_id), now_iso(), weight_kg))


def latest_weight(chat_id):
    rows = q("SELECT weight_kg FROM weights WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 1", (str(chat_id),))
    return rows[0]["weight_kg"] if rows else None


def log_water(chat_id, ml):
    x("INSERT INTO water (chat_id, timestamp, ml) VALUES (?, ?, ?)", (str(chat_id), now_iso(), ml))


def water_today(chat_id) -> int:
    rows = q("SELECT COALESCE(SUM(ml), 0) AS total FROM water WHERE chat_id = ? AND timestamp >= ?",
             (str(chat_id), today_start_iso()))
    return int(rows[0]["total"])


def calories_today(chat_id) -> float:
    rows = q("SELECT COALESCE(SUM(calories), 0) AS total FROM meals WHERE chat_id = ? AND timestamp >= ?",
             (str(chat_id), today_start_iso()))
    return float(rows[0]["total"])


def get_profile(chat_id):
    rows = q("SELECT * FROM profiles WHERE chat_id = ?", (str(chat_id),))
    return rows[0] if rows else None


def local_date(ts_iso: str) -> str:
    """UTC ISO timestamp -> local date string (per TZ_OFFSET_HOURS)."""
    dt = datetime.fromisoformat(ts_iso) + timedelta(hours=TZ_OFFSET_HOURS)
    return dt.strftime("%Y-%m-%d")


def logging_streak(chat_id) -> int:
    """Consecutive local days (ending today or yesterday) with at least one meal logged."""
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    rows = q("SELECT timestamp FROM meals WHERE chat_id = ? AND timestamp >= ?", (str(chat_id), since))
    days = {local_date(r["timestamp"]) for r in rows}
    if not days:
        return 0
    today = (datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)).date()
    # anchor: today if logged today, else yesterday (day isn't over yet)
    anchor = today if today.strftime("%Y-%m-%d") in days else today - timedelta(days=1)
    streak = 0
    d = anchor
    while d.strftime("%Y-%m-%d") in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


def protein_today(chat_id) -> float:
    rows = q("SELECT COALESCE(SUM(protein), 0) AS total FROM meals WHERE chat_id = ? AND timestamp >= ?",
             (str(chat_id), today_start_iso()))
    return float(rows[0]["total"])


def protein_target(chat_id):
    weight = latest_weight(chat_id)
    return int(weight * 1.8) if weight else None


def save_progress_photo(chat_id, file_id):
    x("INSERT INTO progress_photos (chat_id, timestamp, file_id) VALUES (?, ?, ?)",
      (str(chat_id), now_iso(), file_id))


def get_comparison_photo(chat_id, min_days_old=21):
    """Oldest photo at least min_days_old days older than now, for side-by-side comparison."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_days_old)).isoformat()
    rows = q("SELECT file_id, timestamp FROM progress_photos WHERE chat_id = ? AND timestamp <= ? "
             "ORDER BY timestamp ASC LIMIT 1", (str(chat_id), cutoff))
    return rows[0] if rows else None


def send_photo(chat_id, file_id, caption=""):
    requests.post(f"{TELEGRAM_API}/sendPhoto",
                  data={"chat_id": chat_id, "photo": file_id, "caption": caption})


def remove_buttons(chat_id, message_id):
    """Strip inline buttons off an old message so it can't be double-tapped."""
    requests.post(f"{TELEGRAM_API}/editMessageReplyMarkup",
                  data={"chat_id": chat_id, "message_id": message_id,
                        "reply_markup": json.dumps({"inline_keyboard": []})})


def save_profile(chat_id, **kwargs):
    existing = get_profile(chat_id) or {}
    merged = {
        "name": kwargs.get("name", existing.get("name")),
        "age": kwargs.get("age", existing.get("age")),
        "height_cm": kwargs.get("height_cm", existing.get("height_cm")),
        "start_weight_kg": kwargs.get("start_weight_kg", existing.get("start_weight_kg")),
        "sex": kwargs.get("sex", existing.get("sex")),
        "activity": kwargs.get("activity", existing.get("activity")),
    }
    x("INSERT INTO profiles (chat_id, name, age, height_cm, start_weight_kg, sex, activity) "
      "VALUES (?, ?, ?, ?, ?, ?, ?) "
      "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, age=excluded.age, "
      "height_cm=excluded.height_cm, start_weight_kg=excluded.start_weight_kg, "
      "sex=excluded.sex, activity=excluded.activity",
      (str(chat_id), merged["name"], merged["age"], merged["height_cm"], merged["start_weight_kg"],
       merged["sex"], merged["activity"]))


def all_profiles():
    return q("SELECT * FROM profiles ORDER BY name")


def reset_user_data(chat_id):
    for table in ("meals", "workouts", "weights", "water", "profiles", "states", "progress_photos"):
        x(f"DELETE FROM {table} WHERE chat_id = ?", (str(chat_id),))


# ---------- CALORIE TARGETS (Mifflin-St Jeor formula + adaptive from real data) ----------
def _estimated_tdee_from_data(chat_id):
    """Measured maintenance: avg intake minus energy equivalent of weight change.
    Returns None unless there's enough data to trust (≥14 days weight span, ≥7 logged days)."""
    month_ago = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
    weights = q("SELECT weight_kg, timestamp FROM weights WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
                (str(chat_id), month_ago))
    meals = q("SELECT calories, timestamp FROM meals WHERE chat_id = ? AND timestamp >= ?",
              (str(chat_id), month_ago))
    if len(weights) < 4 or not meals:
        return None
    days = (datetime.fromisoformat(weights[-1]["timestamp"]) -
            datetime.fromisoformat(weights[0]["timestamp"])).days
    meal_days = len({local_date(m["timestamp"]) for m in meals})
    if days < 14 or meal_days < 7:
        return None
    # smooth endpoints: average first 3 and last 3 weigh-ins to cut water-weight noise
    first_avg = sum(w["weight_kg"] for w in weights[:3]) / len(weights[:3])
    last_avg = sum(w["weight_kg"] for w in weights[-3:]) / len(weights[-3:])
    weight_change = last_avg - first_avg
    avg_intake = sum(m["calories"] for m in meals) / meal_days
    return avg_intake - (weight_change * 7700 / days)


def compute_targets(chat_id):
    profile = get_profile(chat_id)
    weight = latest_weight(chat_id) or (profile or {}).get("start_weight_kg")
    water_target = int(weight * WATER_ML_PER_KG) if weight else DEFAULT_WATER_TARGET_ML
    if not profile or not all([profile.get("age"), profile.get("height_cm"), weight,
                               profile.get("sex"), profile.get("activity")]):
        return None, None, water_target
    bmr = 10 * weight + 6.25 * profile["height_cm"] - 5 * profile["age"]
    bmr += 5 if profile["sex"] == "m" else -161
    formula_maintenance = int(bmr * profile["activity"])

    # Adaptive: trust measured data when available, capped to ±25% of formula
    # (guards against garbage from incomplete logging)
    maintenance = formula_maintenance
    est = _estimated_tdee_from_data(chat_id)
    if est:
        lo, hi = formula_maintenance * 0.75, formula_maintenance * 1.25
        maintenance = int(min(max(est, lo), hi))

    deficit_target = max(maintenance - DEFICIT_KCAL, MIN_TARGET_KCAL)
    return maintenance, deficit_target, water_target


def targets_are_adaptive(chat_id) -> bool:
    return _estimated_tdee_from_data(chat_id) is not None


# ---------- REPORTS ----------
def send_daily_report(chat_id):
    meals = q("SELECT food_type, food, calories, carbs, protein, fat FROM meals "
              "WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
              (str(chat_id), today_start_iso()))
    maintenance, deficit_target, _ = compute_targets(chat_id)

    if not meals:
        send_message(chat_id, "Daily report, sir: nothing logged today. The abs remain a mystery to me as well.")
        return

    total_cal = sum(m["calories"] for m in meals)
    food_lines = "\n".join(f"- {m['food_type']}: {m['food']} ({m['calories']:.0f} kcal)" for m in meals)
    streak = logging_streak(chat_id)
    p_now = protein_today(chat_id)
    p_target = protein_target(chat_id)

    target_line = ""
    if deficit_target:
        status = "under" if total_cal <= deficit_target else "over"
        target_line = (f"Deficit target: {deficit_target} kcal (maintenance ≈ {maintenance} kcal). "
                       f"You are {abs(total_cal - deficit_target):.0f} kcal {status} target.\n")

    protein_line = f"Protein today: {p_now:.0f}g of ~{p_target}g target.\n" if p_target else ""
    caveat = ("NOTE: only {} item(s) logged today — if meals were skipped in logging, the totals "
              "understate reality; say so.\n".format(len(meals))) if len(meals) < 2 else ""

    prompt = (
        f"{PERSONA_STYLE}"
        "Here is today's food log for someone whose goal is visible, defined abs:\n"
        f"{food_lines}\n"
        f"Total: {total_cal:.0f} kcal. {target_line}{protein_line}{caveat}"
        "Write a short end-of-day report (max 120 words): first, flag which specific logged items "
        "work AGAINST the abs goal (calorie-dense, sugary, fried, alcohol) and briefly why; "
        "note the protein gap if there is one; then note what was good; end with one supportive "
        "line for tomorrow. Plain text only."
    )
    review = call_gemini([{"text": prompt}])
    if not review:
        review = f"Total today: {total_cal:.0f} kcal.\n{target_line}{protein_line}(Analysis engine unavailable.)"
    streak_line = f"\n\n🔥 Logging streak: {streak} day{'s' if streak != 1 else ''}" if streak >= 2 else ""
    send_message(chat_id, f"📋 Daily Report\n\n{review}{streak_line}")


def send_water_reminder(chat_id):
    _, _, water_target = compute_targets(chat_id)
    drunk = water_today(chat_id)
    remaining = max(water_target - drunk, 0)
    if remaining == 0:
        send_message(chat_id, f"💧 Hydration complete, sir — {drunk} ml down, target of {water_target} ml met.")
        return
    send_message(chat_id,
                 f"💧 Hydration check, sir. Progress: {drunk} / {water_target} ml today.\n"
                 f"I'd suggest ~500 ml now — {remaining} ml to go.",
                 buttons=[{"text": "💧 +500 ml", "callback_data": "water_500"},
                          {"text": "+250 ml", "callback_data": "water_250"}])


def send_weekly_report(chat_id):
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    meals = q("SELECT calories, carbs, protein, fat FROM meals WHERE chat_id = ? AND timestamp >= ?",
              (str(chat_id), week_ago))
    workouts = q("SELECT duration, target FROM workouts WHERE chat_id = ? AND timestamp >= ? AND duration != ''",
                 (str(chat_id), week_ago))
    weights = q("SELECT weight_kg, timestamp FROM weights WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
                (str(chat_id), week_ago))

    total_cal = sum(m["calories"] for m in meals)
    total_carbs = sum(m["carbs"] for m in meals)
    total_protein = sum(m["protein"] for m in meals)
    workout_days = len(workouts)

    weight_line = ""
    if len(weights) >= 2:
        # Smooth against water-weight noise: average up to first 3 vs last 3 weigh-ins
        first_avg = sum(w["weight_kg"] for w in weights[:3]) / len(weights[:3])
        last_avg = sum(w["weight_kg"] for w in weights[-3:]) / len(weights[-3:])
        change = last_avg - first_avg
        weight_line = (f"Weight trend this week: {'+' if change >= 0 else ''}{change:.1f} kg "
                       f"(smoothed — single weigh-ins bounce on water alone)\n")

    send_message(chat_id,
                 "📊 Weekly Report, sir\n"
                 f"Meals logged: {len(meals)}\n"
                 f"Avg daily calories: {total_cal / 7:.0f} kcal\n"
                 f"Avg daily carbs: {total_carbs / 7:.0f}g | Avg protein: {total_protein / 7:.0f}g\n"
                 f"Workouts this week: {workout_days}/7\n"
                 f"{weight_line}")


def send_monthly_report(chat_id):
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    meals = q("SELECT food_type, food, calories, carbs, protein, fat, timestamp FROM meals "
              "WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
              (str(chat_id), month_ago))

    if not meals:
        send_message(chat_id, "📅 Monthly report, sir: no food logged in the last 30 days.")
        return

    lines = [f"{m['timestamp'][:10]} | {m['food_type']} | {m['food']} | {m['calories']} kcal | "
             f"{m['carbs']}g carbs | {m['protein']}g protein | {m['fat']}g fat" for m in meals]
    prompt = (
        f"{PERSONA_STYLE}"
        "Here is 30 days of the user's food log (one line per item):\n\n"
        + "\n".join(lines) +
        "\n\nWrite a short monthly eating-habits summary (max 150 words) covering: "
        "average daily calories, the meal/snack/drink balance, most frequently eaten foods, "
        "notable patterns, and one or two practical, supportive suggestions. Plain text only."
    )
    summary = call_gemini([{"text": prompt}])
    if not summary:
        summary = "The analysis engine is momentarily indisposed, sir."
    send_message(chat_id, f"📅 Monthly Eating Habits\n\n{summary}")


def send_body_insights(chat_id):
    month_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    weights = q("SELECT weight_kg, timestamp FROM weights WHERE chat_id = ? AND timestamp >= ? ORDER BY timestamp",
                (str(chat_id), month_ago))
    meals = q("SELECT calories, protein, timestamp FROM meals WHERE chat_id = ? AND timestamp >= ?",
              (str(chat_id), month_ago))
    workouts = q("SELECT duration, target FROM workouts WHERE chat_id = ? AND timestamp >= ?",
                 (str(chat_id), month_ago))
    maintenance, deficit_target, _ = compute_targets(chat_id)

    if len(weights) < 2 or not meals:
        send_message(chat_id,
                     "🧠 Body Insights needs more data, sir — at least two weigh-ins and some logged meals. "
                     "Keep logging for a week or two and I'll have real numbers for you.")
        return

    first_w, last_w = weights[0], weights[-1]
    days = max((datetime.fromisoformat(last_w["timestamp"]) - datetime.fromisoformat(first_w["timestamp"])).days, 1)
    weight_change = last_w["weight_kg"] - first_w["weight_kg"]
    meal_days = len({m["timestamp"][:10] for m in meals})
    avg_intake = sum(m["calories"] for m in meals) / max(meal_days, 1)
    avg_protein = sum(m["protein"] for m in meals) / max(meal_days, 1)
    est_tdee = avg_intake - (weight_change * 7700 / days)
    workout_count = len([w for w in workouts if w["duration"] and w["duration"] != "0"])

    stats = (
        f"Period: last {days} days\n"
        f"Weight: {first_w['weight_kg']:.1f} → {last_w['weight_kg']:.1f} kg "
        f"({'+' if weight_change >= 0 else ''}{weight_change:.1f} kg)\n"
        f"Avg intake on logged days: {avg_intake:.0f} kcal | Avg protein: {avg_protein:.0f} g\n"
        f"Estimated ACTUAL maintenance from your data: ~{est_tdee:.0f} kcal/day\n"
        f"(Formula estimate was ~{maintenance} kcal)\n"
        f"Workouts logged: {workout_count}\n"
    )
    prompt = (
        f"{PERSONA_STYLE}"
        f"Here are the user's measured body statistics:\n{stats}\n"
        "Explain in max 130 words what this data says about HIS body specifically: "
        "how his real-world maintenance compares to the formula, whether his pace of change is sensible "
        "for revealing abs (~0.5% bodyweight/week benchmark), whether protein is sufficient for muscle "
        "retention, and ONE concrete adjustment. Plain text, concrete numbers."
    )
    analysis = call_gemini([{"text": prompt}])
    if not analysis:
        analysis = "Analysis engine unavailable — but the raw numbers above stand on their own, sir."
    send_message(chat_id, f"🧠 Body Insights\n\n{stats}\n{analysis}")


# ---------- HUNGRY ----------
def hungry_reply(chat_id, user_text=None) -> str:
    maintenance, deficit_target, _ = compute_targets(chat_id)
    eaten = calories_today(chat_id)
    if deficit_target:
        budget_line = (f"Today so far: {eaten:.0f} kcal eaten. Daily target: {deficit_target} kcal. "
                       f"Remaining budget: {max(deficit_target - eaten, 0):.0f} kcal.")
    else:
        budget_line = f"Today so far: {eaten:.0f} kcal eaten. (No profile — suggest ~500-700 kcal meals.)"

    weight = latest_weight(chat_id)
    protein_line = f"His daily protein target is roughly {int(weight * 1.8)} g (1.8 g/kg). " if weight else ""
    request_line = (f"The user's specific request: \"{user_text}\". " if user_text
                    else "No specific request — offer 2-3 options. ")
    prompt = (
        f"{PERSONA_STYLE}"
        "The user is hungry and wants SIMPLE meal ideas that a home cook/house help can prepare with "
        "everyday ingredients — nothing fancy, max 5-6 ingredients. "
        f"{budget_line} {protein_line}{request_line}"
        "Give 2-3 concrete options, each with: name, one-line how-to, approximate kcal and protein grams. "
        "Prioritize high protein and high satiety within the remaining budget. "
        "Plain text, max 130 words. End by inviting a follow-up."
    )
    return call_gemini([{"text": prompt}]) or "The kitchen brain is momentarily offline, sir."


# ---------- VOICE ----------
def process_voice_note(chat_id, audio_bytes, mime_type):
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    raw = call_gemini([
        {"text": VOICE_EXTRACT_PROMPT},
        {"inline_data": {"mime_type": mime_type, "data": audio_b64}},
    ])
    if not raw:
        send_message(chat_id, "I couldn't process that voice note, sir. Do try once more.")
        return
    raw = raw.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        send_message(chat_id, "I heard you, sir, but couldn't structure it. Try stating it plainly, e.g. "
                              "\"I had two eggs and toast, and did 30 minutes of abs.\"")
        return
    if not items:
        send_message(chat_id, "Nothing loggable detected in that note, sir.")
        return

    replies = []
    for item in items:
        if item.get("kind") == "food":
            result = {"food": item.get("food", "unknown"), "calories": item.get("calories", 0),
                      "carbs": item.get("carbs", 0), "protein": item.get("protein", 0),
                      "fat": item.get("fat", 0), "confidence": item.get("confidence", "low")}
            ftype = item.get("food_type", "meal")
            meal_id = log_meal(chat_id, ftype, result)
            set_state(chat_id, state="can_correct", last_meal_id=meal_id)
            replies.append(f"🍽 {ftype}: {result['food']} — {result['calories']:.0f} kcal, "
                           f"{result['protein']:.0f}g protein")
        elif item.get("kind") == "workout":
            log_workout(chat_id, item.get("duration", ""), item.get("target", ""))
            replies.append(f"💪 workout: {item.get('duration', '?')} min, {item.get('target', '?')}")

    send_message(chat_id, "Logged from your voice note, sir:\n" + "\n".join(replies) +
                          "\n\n(If any estimate is off, reply with a correction.)")


# ---------- ADMIN ----------
def show_admin_user_list(chat_id):
    profiles = all_profiles()
    if not profiles:
        send_message(chat_id, "No registered users yet.")
        return
    rows, row = [], []
    for p in profiles:
        label = p.get("name") or p["chat_id"]
        row.append({"text": f"👤 {label}", "callback_data": f"admin_view_{p['chat_id']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    send_message(chat_id, "🔑 Admin — registered users:", button_rows=rows)


def show_admin_user_detail(admin_chat_id, target_chat_id):
    profile = get_profile(target_chat_id)
    name = (profile or {}).get("name") or target_chat_id
    meals_today = q("SELECT food_type, food, calories FROM meals WHERE chat_id = ? AND timestamp >= ?",
                    (str(target_chat_id), today_start_iso()))
    weight = latest_weight(target_chat_id) or (profile or {}).get("start_weight_kg")
    maintenance, deficit_target, water_target = compute_targets(target_chat_id)
    drunk = water_today(target_chat_id)

    lines = [f"📋 {name}'s Day\n"]
    if profile:
        lines.append(f"Age {profile.get('age', '—')} | {weight or '—'} kg | {profile.get('height_cm', '—')} cm")
    if maintenance:
        lines.append(f"Target: ~{deficit_target} kcal | Maintenance: ~{maintenance} kcal")
    lines.append(f"Water: {drunk} / {water_target} ml today\n")
    if meals_today:
        lines.append("Today's food:")
        total = 0
        for m in meals_today:
            lines.append(f"- {m['food_type']}: {m['food']} ({m['calories']:.0f} kcal)")
            total += m["calories"]
        lines.append(f"\nTotal: {total:.0f} kcal")
    else:
        lines.append("Nothing logged today yet.")
    send_message(admin_chat_id, "\n".join(lines),
                 buttons=[{"text": "⬅️ Back to list", "callback_data": "admin_list"}])


# ---------- MENU / PROFILE / CHAT ----------
def show_menu(chat_id):
    profile = get_profile(chat_id)
    name = (profile or {}).get("name")
    greeting = f"At your service, {name}. What shall it be?" if name else MENU_TEXT
    send_message(chat_id, greeting,
                 button_rows=[MENU_BUTTONS, MENU_BUTTONS_ROW2, MENU_BUTTONS_ROW3, MENU_BUTTONS_ROW4])
    send_message(chat_id, "Quick actions below ⬇️", with_keyboard=True)


def show_profile(chat_id):
    profile = get_profile(chat_id)
    if not profile or not profile.get("age"):
        set_state(chat_id, state="awaiting_name")
        send_message(chat_id, "👤 Profile setup, sir. First — what shall I call you?")
        return
    weight = latest_weight(chat_id) or profile.get("start_weight_kg")
    maintenance, deficit_target, water_target = compute_targets(chat_id)
    sex_label = {"m": "Male", "f": "Female"}.get(profile.get("sex"), "—")
    act_label = {1.2: "Sedentary", 1.375: "Light", 1.55: "Moderate", 1.725: "Very active"}.get(
        profile.get("activity"), "—")
    text = (
        f"👤 Profile — {profile.get('name') or 'Unnamed'}\n"
        f"Age: {profile.get('age')} | Height: {profile.get('height_cm'):.0f} cm\n"
        f"Start weight: {profile.get('start_weight_kg'):.1f} kg | Current: {weight:.1f} kg\n"
        f"Sex: {sex_label} | Activity: {act_label}\n"
    )
    if maintenance:
        adaptive_tag = " (adaptive — measured from your own data)" if targets_are_adaptive(chat_id) else " (formula estimate)"
        text += (f"\n🔢 Maintenance: ~{maintenance} kcal{adaptive_tag}\n"
                 f"🎯 Target: ~{deficit_target} kcal\n"
                 f"💧 Water target: ~{water_target} ml/day")
    send_message(chat_id, text, button_rows=[
        [{"text": "✏️ Name", "callback_data": "edit_name"},
         {"text": "✏️ Age", "callback_data": "edit_age"},
         {"text": "✏️ Height", "callback_data": "edit_height"}],
        [{"text": "✏️ Activity", "callback_data": "edit_activity"},
         {"text": "📸 Progress photo", "callback_data": "progress_photo"}],
        [{"text": "🔄 Redo full setup", "callback_data": "edit_full"}],
    ])


def chat_reply(chat_id, text) -> str:
    profile = get_profile(chat_id) or {}
    maintenance, deficit_target, water_target = compute_targets(chat_id)
    eaten = calories_today(chat_id)
    context = (
        f"User's name: {profile.get('name') or 'unknown'}. "
        f"Today's intake so far: {eaten:.0f} kcal. "
        + (f"Daily target: {deficit_target} kcal, maintenance {maintenance} kcal. " if deficit_target else "")
        + f"Water target: {water_target} ml. "
    )
    prompt = (
        f"{PERSONA_STYLE}"
        f"Context: {context}\n"
        f"The user said: \"{text}\"\n"
        "Reply conversationally in 1-3 short sentences maximum. If they're asking about fitness/food/their "
        "progress, answer with concrete numbers from the context. If they want to log something, remind them "
        "of the quick action buttons or that they can send a photo/voice note. Plain text only."
    )
    return call_gemini([{"text": prompt}]) or "I'm momentarily lost for words, sir. Try the 📋 Menu."


# ---------- WEBHOOK ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    print(f"INCOMING UPDATE: {update}", flush=True)

    # ----- Voice note -----
    if "message" in update and ("voice" in update["message"] or "audio" in update["message"]):
        chat_id = update["message"]["chat"]["id"]
        try:
            media = update["message"].get("voice") or update["message"].get("audio")
            audio_bytes = download_telegram_file(media["file_id"])
            process_voice_note(chat_id, audio_bytes, media.get("mime_type", "audio/ogg"))
        except Exception as e:
            print(f"VOICE HANDLING FAILED: {type(e).__name__}: {e}", flush=True)
            send_message(chat_id, f"My apologies, sir — that voice note defeated me: {e}")
        return jsonify(ok=True)

    # ----- Photo -----
    if "message" in update and "photo" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        try:
            state = get_state(chat_id)
            file_id = update["message"]["photo"][-1]["file_id"]

            # Progress photo mode (after the weekly prompt or 📸 button)
            if state.get("state") == "awaiting_progress_photo":
                save_progress_photo(chat_id, file_id)
                set_state(chat_id, state="")
                old = get_comparison_photo(chat_id)
                send_message(chat_id, "📸 Progress photo saved, sir. The mirror never lies — but it does take a few weeks to speak up.")
                if old and old["file_id"] != file_id:
                    send_photo(chat_id, old["file_id"],
                               caption=f"For comparison, sir — you on {local_date(old['timestamp'])}.")
                return jsonify(ok=True)

            food_type = state.get("pending_food_type") or "meal"
            image_bytes = download_telegram_file(file_id)
            result = analyze_food_image(image_bytes, "image/jpeg")
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

        if text.lower() in {"/myid", "myid"}:
            send_message(chat_id, f"Your chat ID, sir: {chat_id}")
            return jsonify(ok=True)

        if text.lower() in {"/admin", "admin"}:
            if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
                show_admin_user_list(chat_id)
            else:
                send_message(chat_id, "Not authorized for that, sir.")
            return jsonify(ok=True)

        if text.lower() in {"/undo", "undo"}:
            removed = delete_last_meal(chat_id)
            if removed:
                set_state(chat_id, state="", last_meal_id=0)
                send_message(chat_id, f"🗑 Removed: {removed}. As if it never happened, sir.")
            else:
                send_message(chat_id, "Nothing to undo, sir.")
            return jsonify(ok=True)

        # Persistent keyboard quick actions
        if text == "🍽 Log Food":
            send_message(chat_id, FOOD_TYPE_TEXT, buttons=FOOD_TYPE_BUTTONS)
            return jsonify(ok=True)
        if text == "💪 Workout":
            send_message(chat_id, DAILY_PROMPT_TEXT, button_rows=[DURATION_OPTIONS, DURATION_OPTIONS_ROW2])
            return jsonify(ok=True)
        if text == "💧 Water +500":
            log_water(chat_id, 500)
            _, _, water_target = compute_targets(chat_id)
            send_message(chat_id, f"💧 Logged 500 ml. {water_today(chat_id)} / {water_target} ml today.")
            return jsonify(ok=True)
        if text == "⚖️ Weight":
            set_state(chat_id, state="awaiting_weight")
            send_message(chat_id, "Your weight in kg, sir (just the number, e.g. 72.5).")
            return jsonify(ok=True)
        if text == "🍳 Hungry":
            set_state(chat_id, state="hungry_chat", pending_food_type="")
            send_message(chat_id, hungry_reply(chat_id))
            return jsonify(ok=True)
        if text == "📋 Menu":
            set_state(chat_id, state="", pending_food_type="")
            show_menu(chat_id)
            return jsonify(ok=True)

        if text.lower() in GREETING_WORDS:
            set_state(chat_id, state="", pending_food_type="")
            show_menu(chat_id)
            return jsonify(ok=True)

        if state.get("state") == "awaiting_name":
            save_profile(chat_id, name=text[:40])
            set_state(chat_id, state="awaiting_age")
            send_message(chat_id, f"A pleasure, {text[:40]}. How old are you?")
            return jsonify(ok=True)

        if state.get("state") == "edit_name":
            save_profile(chat_id, name=text[:40])
            set_state(chat_id, state="")
            show_profile(chat_id)
            return jsonify(ok=True)
        if state.get("state") == "edit_age":
            try:
                save_profile(chat_id, age=int(text))
                set_state(chat_id, state="")
                show_profile(chat_id)
            except ValueError:
                send_message(chat_id, "A whole number please, sir — e.g. 21")
            return jsonify(ok=True)
        if state.get("state") == "edit_height":
            try:
                save_profile(chat_id, height_cm=float(text.replace(",", ".")))
                set_state(chat_id, state="")
                show_profile(chat_id)
            except ValueError:
                send_message(chat_id, "Height in cm please, sir — e.g. 175")
            return jsonify(ok=True)

        if state.get("state") == "awaiting_age":
            try:
                save_profile(chat_id, age=int(text))
                set_state(chat_id, state="awaiting_height")
                send_message(chat_id, "Noted. And your height in cm, sir?")
            except ValueError:
                send_message(chat_id, "Your age as a whole number please, sir — e.g. 21")
            return jsonify(ok=True)

        if state.get("state") == "awaiting_height":
            try:
                save_profile(chat_id, height_cm=float(text.replace(",", ".")))
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
                             buttons=[{"text": "Male", "callback_data": "sex_m"},
                                      {"text": "Female", "callback_data": "sex_f"}])
            except ValueError:
                send_message(chat_id, "Just the number please, sir — e.g. 72.5")
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

        if state.get("state") == "hungry_chat":
            send_message(chat_id, hungry_reply(chat_id, user_text=text))
            return jsonify(ok=True)

        if state.get("state") == "can_correct" and state.get("last_meal_id"):
            rows = q("SELECT * FROM meals WHERE id = ?", (state["last_meal_id"],))
            if rows:
                row = rows[0]
                original = {"food": row["food"], "calories": row["calories"], "carbs": row["carbs"],
                            "protein": row["protein"], "fat": row["fat"], "confidence": row["confidence"]}
                revised = reanalyze_with_correction(original, text)
                update_meal(state["last_meal_id"], revised)
                send_message(chat_id, "Amended, sir ✅\n" + format_food_reply(revised, row["food_type"]))
            else:
                send_message(chat_id, "I couldn't locate the last entry to amend, sir.")
            return jsonify(ok=True)

        send_message(chat_id, chat_reply(chat_id, text))
        return jsonify(ok=True)

    # ----- Button taps -----
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq["data"]
        answer_callback(cq["id"])

        # One-shot buttons: remove them after the tap so old messages can't double-log
        one_shot_prefixes = ("water_", "dur_", "tgt_", "int_", "ft_", "sex_", "act_", "demo_reset")
        if data.startswith(one_shot_prefixes):
            try:
                remove_buttons(chat_id, message_id)
            except Exception:
                pass

        if data == "menu_food":
            send_message(chat_id, FOOD_TYPE_TEXT, buttons=FOOD_TYPE_BUTTONS)
        elif data == "menu_workout":
            send_message(chat_id, DAILY_PROMPT_TEXT, button_rows=[DURATION_OPTIONS, DURATION_OPTIONS_ROW2])
        elif data == "menu_weight":
            set_state(chat_id, state="awaiting_weight")
            send_message(chat_id, "Your weight in kg, sir (just the number, e.g. 72.5).")
        elif data == "menu_profile":
            show_profile(chat_id)
        elif data == "edit_name":
            set_state(chat_id, state="edit_name")
            send_message(chat_id, "What shall I call you, sir?")
        elif data == "edit_age":
            set_state(chat_id, state="edit_age")
            send_message(chat_id, "Your age?")
        elif data == "edit_height":
            set_state(chat_id, state="edit_height")
            send_message(chat_id, "Your height in cm?")
        elif data == "edit_activity":
            send_message(chat_id, "How active are you day to day, sir?",
                         button_rows=[[{"text": "Sedentary", "callback_data": "act_1.2"},
                                       {"text": "Light", "callback_data": "act_1.375"},
                                       {"text": "Moderate", "callback_data": "act_1.55"}],
                                      [{"text": "Very active", "callback_data": "act_1.725"}]])
        elif data == "edit_full":
            set_state(chat_id, state="awaiting_name")
            send_message(chat_id, "Full setup then, sir. What shall I call you?")
        elif data == "progress_photo":
            set_state(chat_id, state="awaiting_progress_photo")
            send_message(chat_id, "📸 Send your progress photo, sir — same spot, same lighting each time.")
        elif data == "menu_hungry":
            set_state(chat_id, state="hungry_chat", pending_food_type="")
            send_message(chat_id, "One moment, sir — consulting the pantry and your calorie ledger...")
            send_message(chat_id, hungry_reply(chat_id))
        elif data == "menu_insights":
            send_message(chat_id, "Crunching your numbers, sir...")
            send_body_insights(chat_id)
        elif data == "menu_demo":
            send_message(chat_id, "Testing chamber, sir. Which system shall I fire?",
                         button_rows=[[{"text": "📋 Daily Report", "callback_data": "demo_daily"},
                                       {"text": "💧 Water Reminder", "callback_data": "demo_water"}],
                                      [{"text": "📊 Weekly Report", "callback_data": "demo_weekly"},
                                       {"text": "📅 Monthly Report", "callback_data": "demo_monthly"}],
                                      [{"text": "🗑 Reset My Data", "callback_data": "demo_reset"}]])
        elif data.startswith("ft_"):
            food_type = data.replace("ft_", "")
            set_state(chat_id, state="", pending_food_type=food_type)
            send_message(chat_id, f"Send a photo of your {food_type}, sir 📸\n"
                                  f"(Tip: include a fork or your hand in frame — it sharpens my portion estimates.)")
        elif data.startswith("dur_"):
            duration = data.replace("dur_", "")
            if duration == "0":
                log_workout(chat_id, "0", "Rest day", "")
                send_message(chat_id, "A rest day then, sir. Recovery is training too.")
            else:
                set_state(chat_id, pending_duration=duration)
                send_message(chat_id, TARGET_PROMPT_TEXT,
                             button_rows=[TARGET_OPTIONS, TARGET_OPTIONS_ROW2, TARGET_OPTIONS_ROW3])
        elif data.startswith("tgt_"):
            target = data.replace("tgt_", "")
            if target == "Rest day":
                log_workout(chat_id, get_state(chat_id).get("pending_duration", ""), target, "")
                set_state(chat_id, pending_duration="", pending_target="")
                send_message(chat_id, "Rest day logged, sir. Well earned, I trust.")
            else:
                set_state(chat_id, pending_target=target)
                send_message(chat_id, INTENSITY_PROMPT_TEXT, buttons=INTENSITY_OPTIONS)
        elif data.startswith("int_"):
            intensity = data.replace("int_", "")
            st = get_state(chat_id)
            log_workout(chat_id, st.get("pending_duration", ""), st.get("pending_target", ""), intensity)
            set_state(chat_id, pending_duration="", pending_target="")
            send_message(chat_id,
                         f"💪 Logged: {st.get('pending_duration', '?')} min {st.get('pending_target', '?')}, "
                         f"{intensity.lower()} intensity. Well done, sir.")
        elif data.startswith("sex_"):
            save_profile(chat_id, sex=data.replace("sex_", ""))
            send_message(chat_id, "And how active are you day to day, sir?",
                         button_rows=[[{"text": "Sedentary", "callback_data": "act_1.2"},
                                       {"text": "Light", "callback_data": "act_1.375"},
                                       {"text": "Moderate", "callback_data": "act_1.55"}],
                                      [{"text": "Very active", "callback_data": "act_1.725"}]])
        elif data.startswith("act_"):
            save_profile(chat_id, activity=float(data.replace("act_", "")))
            maintenance, deficit_target, water_target = compute_targets(chat_id)
            send_message(chat_id,
                         f"Profile complete, sir ✅\n\n"
                         f"Estimated maintenance: ~{maintenance} kcal/day\n"
                         f"Recommended target for the abs project: ~{deficit_target} kcal/day "
                         f"(a moderate {DEFICIT_KCAL} kcal deficit — sustainable beats drastic)\n"
                         f"Daily water target: ~{water_target} ml\n\n"
                         f"These are estimates, sir — we'll refine as the weigh-ins come in.")
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
            send_message(chat_id, "This erases ALL your logged data, sir — quite irreversible. Certain?",
                         buttons=[{"text": "Yes, wipe it", "callback_data": "demo_reset_confirm"},
                                  {"text": "Cancel", "callback_data": "demo_reset_cancel"}])
        elif data == "demo_reset_confirm":
            reset_user_data(chat_id)
            send_message(chat_id, "Done, sir. A blank slate. Send 'menu' to begin anew.")
        elif data == "demo_reset_cancel":
            send_message(chat_id, "Wise choice, sir. Data intact.")
        elif data == "admin_list":
            if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
                show_admin_user_list(chat_id)
        elif data.startswith("admin_view_"):
            if ADMIN_CHAT_ID and str(chat_id) == str(ADMIN_CHAT_ID):
                show_admin_user_detail(chat_id, data.replace("admin_view_", ""))
        return jsonify(ok=True)

    return jsonify(ok=True)


# ---------- TRIGGERS ----------
def _for_all_users(fn):
    if not FRIEND_CHAT_IDS:
        return jsonify(status="no chat ids set"), 400
    for cid in FRIEND_CHAT_IDS:
        try:
            fn(cid)
        except Exception as e:
            print(f"TRIGGER FAILED for {cid}: {type(e).__name__}: {e}", flush=True)
    return jsonify(status="sent", users=len(FRIEND_CHAT_IDS)), 200


@app.route("/trigger/daily-prompt", methods=["GET", "POST"])
def trigger_daily_prompt():
    return _for_all_users(lambda cid: send_message(cid, DAILY_PROMPT_TEXT,
                                                   button_rows=[DURATION_OPTIONS, DURATION_OPTIONS_ROW2]))


@app.route("/trigger/daily-report", methods=["GET", "POST"])
def trigger_daily_report():
    return _for_all_users(send_daily_report)


@app.route("/trigger/water-reminder", methods=["GET", "POST"])
def trigger_water_reminder():
    return _for_all_users(send_water_reminder)


def _send_weight_reminder(cid):
    set_state(cid, state="awaiting_weight")
    send_message(cid, "⚖️ Morning weigh-in, sir. For consistency: after waking, after the bathroom, "
                      "before food or drink.\n\nReply with your weight in kg (just the number).")


@app.route("/trigger/weight-reminder", methods=["GET", "POST"])
def trigger_weight_reminder():
    return _for_all_users(_send_weight_reminder)


@app.route("/trigger/weekly-report", methods=["GET", "POST"])
def trigger_weekly_report():
    return _for_all_users(send_weekly_report)


@app.route("/trigger/monthly-report", methods=["GET", "POST"])
def trigger_monthly_report():
    return _for_all_users(send_monthly_report)


def _send_protein_check(cid):
    p_target = protein_target(cid)
    if not p_target:
        return
    p_now = protein_today(cid)
    gap = p_target - p_now
    if gap <= 20:
        return  # on track, don't nag
    send_message(cid,
                 f"🥩 Protein check, sir: {p_now:.0f}g of {p_target}g so far — {gap:.0f}g short with the "
                 f"evening ahead. A shake (~25g), 200g chicken breast (~45g), or 250g Greek yogurt (~25g) "
                 f"would close the gap nicely.")


@app.route("/trigger/protein-check", methods=["GET", "POST"])
def trigger_protein_check():
    return _for_all_users(_send_protein_check)


def _send_photo_prompt(cid):
    set_state(cid, state="awaiting_progress_photo")
    send_message(cid,
                 "📸 Weekly progress photo, sir. Same spot, same lighting, relaxed front pose — "
                 "consistency is what makes the comparison honest. Send it whenever ready.")


@app.route("/trigger/photo-prompt", methods=["GET", "POST"])
def trigger_photo_prompt():
    return _for_all_users(_send_photo_prompt)


@app.route("/", methods=["GET"])
def health():
    return jsonify(status="alive", db="turso" if USE_TURSO else "sqlite-ephemeral"), 200


# ---------- iOS SHORTCUTS ----------
def _shortcut_auth_ok():
    if not SHORTCUT_TOKEN:
        return True
    return request.args.get("token", "") == SHORTCUT_TOKEN or request.form.get("token", "") == SHORTCUT_TOKEN


@app.route("/shortcut/water", methods=["GET", "POST"])
def shortcut_water():
    if not _shortcut_auth_ok():
        return "Unauthorized", 401
    chat_id = request.args.get("chat_id") or request.form.get("chat_id")
    if not chat_id:
        return "Missing chat_id", 400
    ml = int(request.args.get("ml", request.form.get("ml", 500)))
    log_water(chat_id, ml)
    _, _, water_target = compute_targets(chat_id)
    return f"💧 Logged {ml} ml. Today: {water_today(chat_id)} / {water_target} ml.", 200


@app.route("/shortcut/meal", methods=["POST"])
def shortcut_meal():
    if not _shortcut_auth_ok():
        return "Unauthorized", 401
    chat_id = request.args.get("chat_id") or request.form.get("chat_id")
    if not chat_id:
        return "Missing chat_id", 400
    if "photo" not in request.files:
        return "Missing photo file (form field name must be 'photo')", 400
    f = request.files["photo"]
    result = analyze_food_image(f.read(), f.mimetype or "image/jpeg")
    meal_id = log_meal(chat_id, request.form.get("food_type", "meal"), result)
    set_state(chat_id, state="can_correct", pending_food_type="", last_meal_id=meal_id)
    return (f"Logged: {result.get('food')}\n"
            f"{result.get('calories')} kcal | {result.get('carbs')}g C | "
            f"{result.get('protein')}g P | {result.get('fat')}g F", 200)


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    render_url = request.args.get("url")
    if not render_url:
        return jsonify(error="pass ?url=https://your-app.onrender.com"), 400
    r = requests.get(f"{TELEGRAM_API}/setWebhook", params={"url": f"{render_url}/webhook"})
    return jsonify(r.json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
