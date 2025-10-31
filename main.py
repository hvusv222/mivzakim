import os
import json
import subprocess
import requests
import base64
from datetime import datetime, timedelta
import pytz
import asyncio
import re
from difflib import SequenceMatcher
import wave
import webrtcvad
import time
import random
from telegram.ext import filters

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler
from google.cloud import texttospeech

# 📁 קובץ לשמירת היסטוריית הודעות
LAST_MESSAGES_FILE = "last_messages.json"
MAX_HISTORY = 55

# 📁 קובץ הגדרות סינון
FILTERS_FILE = "filters.json"
BLOCKED_PHRASES = []
STRICT_BANNED = []
WORD_BANNED = []
ALLOWED_LINKS = []
# ✅ חדש: רשימת מספרי טלפון מאושרים
ALLOWED_PHONES = [] 

# ✅ תוספת חדשה: קובץ הגדרות החלפת מילים
REPLACEMENTS_FILE = "replacements.json"
WORD_REPLACEMENTS = {} # יכיל מילון, לדוגמה: {"ה": "השם"}

# ✅ חדש: ביטוי רגולרי לזיהוי מספרי טלפון
# דוגמאות למה שנתפס: 050-1234567, 03 1234567, 1700-123456
PHONE_NUMBER_REGEX = re.compile(r'\b(0\d{1,2}[-\s]?\d{7}|1[5-9]00[-\s]?\d{6}|05\d[-\s]?\d{7})\b')

# ✅ חדש: מיפוי שמות פשוטים למפתחות JSON (עבור פילטרים)
FILTER_MAPPING = {
    "ניקוי": "BLOCKED_PHRASES",
    "איסור-חזק": "STRICT_BANNED",
    "איסור-מילה": "WORD_BANNED",
    "קישורים": "ALLOWED_LINKS",
    "מספרים-מאושרים": "ALLOWED_PHONES" # ✅ חדש
}

def load_last_messages():
    if not os.path.exists(LAST_MESSAGES_FILE):
        return []
    try:
        with open(LAST_MESSAGES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ שגיאה בטעינת היסטוריית הודעות: {e}")
        return []

def save_last_messages(messages):
    messages = messages[-MAX_HISTORY:]
    try:
        with open(LAST_MESSAGES_FILE, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ שגיאה בשמירת היסטוריית הודעות: {e}")

# ⚙️ פונקציה לטעינת הגדרות הסינון
def load_filters():
    global BLOCKED_PHRASES, STRICT_BANNED, WORD_BANNED, ALLOWED_LINKS, ALLOWED_PHONES
    
    # הגדרות ברירת מחדל מלאות
    default_data = {
        "BLOCKED_PHRASES": [],
        "STRICT_BANNED": [],
        "WORD_BANNED": [],
        "ALLOWED_LINKS": [],
        "ALLOWED_PHONES": [] # ✅ חדש
    }

    if not os.path.exists(FILTERS_FILE):
        # יצירת קובץ ברירת מחדל אם אינו קיים
        with open(FILTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_data, f, ensure_ascii=False, indent=4)
        
        # אם נוצר חדש, נשתמש בברירת המחדל
        BLOCKED_PHRASES = default_data["BLOCKED_PHRASES"]
        STRICT_BANNED = default_data["STRICT_BANNED"]
        WORD_BANNED = default_data["WORD_BANNED"]
        ALLOWED_LINKS = default_data["ALLOWED_LINKS"]
        ALLOWED_PHONES = default_data["ALLOWED_PHONES"]
        print("✅ נוצר קובץ הגדרות ברירת מחדל חדש.")
        return default_data

    try:
        with open(FILTERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # עדכון הרשימות הגלובליות תוך שימוש ב-default_data כמקור אם מפתח חסר
        BLOCKED_PHRASES = sorted(data.get("BLOCKED_PHRASES", default_data["BLOCKED_PHRASES"]), key=len, reverse=True)
        STRICT_BANNED = data.get("STRICT_BANNED", default_data["STRICT_BANNED"])
        WORD_BANNED = data.get("WORD_BANNED", default_data["WORD_BANNED"])
        ALLOWED_LINKS = data.get("ALLOWED_LINKS", default_data["ALLOWED_LINKS"])
        ALLOWED_PHONES = data.get("ALLOWED_PHONES", default_data["ALLOWED_PHONES"]) # ✅ טעינה

        print(f"✅ נטענו בהצלחה {len(BLOCKED_PHRASES)} ניקוי, {len(STRICT_BANNED)} פוסלים, {len(WORD_BANNED)} מילים, {len(ALLOWED_LINKS)} קישורים ו- {len(ALLOWED_PHONES)} מספרים מאושרים.")
        return data
    except Exception as e:
        print(f"❌ נכשל בטעינת קובץ הגדרות סינון: {e}")
        return None

# ✅ חדש: פונקציה לשמירת הגדרות הסינון
def save_filters(data):
    try:
        # לוודא שכל הרשימות נשמרות לפי המפתחות שלהן
        filtered_data = {k: data.get(k, []) for k in FILTER_MAPPING.values()}
        with open(FILTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(filtered_data, f, ensure_ascii=False, indent=4)
        print("✅ הגדרות הסינון נשמרו בהצלחה.")
        return True
    except Exception as e:
        print(f"❌ שגיאה בשמירת הגדרות סינון: {e}")
        return False

# ✅ תוספת חדשה: פונקציה לטעינת החלפות מילים
def load_replacements():
    global WORD_REPLACEMENTS
    default_data = {} # ברירת המחדל היא מילון ריק
    
    if not os.path.exists(REPLACEMENTS_FILE):
        with open(REPLACEMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_data, f, ensure_ascii=False, indent=4)
        WORD_REPLACEMENTS = default_data
        print("✅ נוצר קובץ החלפות מילים חדש (ריק).")
        return default_data
    
    try:
        with open(REPLACEMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # לוודא שזה מילון
        if not isinstance(data, dict):
             raise Exception("הקובץ אינו מכיל מילון (אובייקט JSON)")
        WORD_REPLACEMENTS = data
        print(f"✅ נטענו בהצלחה {len(WORD_REPLACEMENTS)} החלפות מילים.")
        return data
    except Exception as e:
        print(f"❌ נכשל בטעינת קובץ החלפות: {e}. משתמש במילון ריק.")
        WORD_REPLACEMENTS = default_data
        return default_data

# ✅ תוספת חדשה: פונקציה לשמירת החלפות מילים
def save_replacements(data):
    global WORD_REPLACEMENTS
    if not isinstance(data, dict):
        print("❌ שגיאה: ניסיון לשמור החלפות שאינן מילון.")
        return False
        
    WORD_REPLACEMENTS = data # עדכון המשתנה הגלובלי
    try:
        with open(REPLACEMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        print("✅ החלפות המילים נשמרו בהצלחה.")
        return True
    except Exception as e:
        print(f"❌ שגיאה בשמירת החלפות מילים: {e}")
        return False

# 🟡 כתיבת קובץ מפתח Google מ־BASE64
key_b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_B64")
if not key_b64:
    raise Exception("❌ משתנה GOOGLE_APPLICATION_CREDENTIALS_B64 לא מוגדר או ריק")

try:
    with open("google_key.json", "wb") as f:
        f.write(base64.b64decode(key_b64))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_key.json"
except Exception as e:
    raise Exception("❌ נכשל בכתיבת קובץ JSON מ־BASE64: " + str(e))

# 🛠 משתנים מ־Render וחדשים
BOT_TOKEN = os.getenv("BOT_TOKEN")
YMOT_TOKEN = os.getenv("YMOT_TOKEN")
YMOT_PATH = os.getenv("YMOT_PATH", "ivr2:90/")
# ✅ חדש: מזהה משתמש אדמין לשליטה בפילטרים
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID") # מומלץ להגדיר כמשתנה סביבה!

# טוען את הפילטרים מיד לאחר הגדרת המשתנים הגלובליים
try:
    filter_data = load_filters()
except Exception as e:
    print(e)
    # אפשרות להמשיך עם רשימות ריקות אם הטעינה נכשלה, או לזרוק את השגיאה
    pass

# ✅ תוספת חדשה: טעינת החלפות המילים בהפעלה
try:
    replacements_data = load_replacements()
except Exception as e:
    print(e)
    pass


# 🔒 פונקציה לבדיקת הרשאת אדמין
def is_admin(user_id):
    if not ADMIN_USER_ID:
        # אם אין ADMIN_USER_ID מוגדר, אף אחד לא אדמין
        return False
    # בדיקה אם המשתמש הוא האדמין המוגדר (ADMIN_USER_ID הוא סטרינג)
    return str(user_id) == ADMIN_USER_ID

# 🔢 המרת מספרים לעברית
def num_to_hebrew_words(hour, minute):
    hours_map = {
        1: "אחת", 2: "שתיים", 3: "שלוש", 4: "ארבע", 5: "חמש",
        6: "שש", 7: "שבע", 8: "שמונה", 9: "תשע", 10: "עשר",
        11: "אחת עשרה", 12: "שתים עשרה"
    }
    minutes_map = {
        0: "", 1: "ודקה", 2: "ושתי דקות", 3: "ושלוש דקות", 4: "וארבע דקות", 5: "וחמישה",
        6: "ושש דקות", 7: "ושבע דקות", 8: "ושמונה דקות", 9: "ותשע דקות", 10: "ועשרה",
        11: "ואחת עשרה דקות", 12: "ושתים עשרה דקות", 13: "ושלוש עשרה דקות", 14: "וארבע עשרה דקות",
        15: "ורבע", 16: "ושש עשרה דקות", 17: "ושבע עשרה דקות", 18: "ושמונה עשרה דקות",
        19: "ותשע עשרה דקות", 20: "ועשרים", 21: "עשרים ואחת", 22: "עשרים ושתיים",
        23: "עשרים ושלוש", 24: "עשרים וארבע", 25: "עשרים וחמש", 26: "עשרים ושש",
        27: "עשרים ושבע", 28: "עשרים ושמונה", 29: "עשרים ותשע", 30: "וחצי",
        31: "שלושים ואחת", 32: "שלושים ושתיים", 33: "שלושים ושלוש",
        34: "שלושים וארבע", 35: "שלושים וחמש", 36: "שלושים ושש",
        37: "שלושים ושבע", 38: "שלושים ושמונה", 39: "שלושים ותשע",
        40: "וארבעים דקות", 41: "ארבעים ואחת", 42: "ארבעים ושתיים",
        43: "ארבעים ושלוש", 44: "ארבעים וארבע", 45: "ארבעים וחמש",
        46: "ארבעים ושש", 47: "ארבעים ושבע", 48: "ארבעים ושמונה",
        49: "ארבעים ותשע", 50: "וחמישים דקות", 51: "חמישים ואחת",
        52: "חמישים ושתיים", 53: "חמישים ושלוש", 54: "חמישים וארבע",
        55: "חמישים וחמש", 56: "חמישים ושש", 57: "חמישים ושבע",
        58: "חמישים ושמונה", 59: "חמישים ותשע"
    }
    hour_12 = hour % 12 or 12
    return f"{hours_map[hour_12]} {minutes_map[minute]}"

def clean_text(text):
    add_moked_credit = False

    # בדיקה אם ההודעה מתחילה במילים 'חדשות המוקד'
    if text.strip().startswith("חדשות המוקד"):
        add_moked_credit = True
        
    # --- ✅ בדיקה ראשונה: האם יש מספר טלפון? ---
    global PHONE_NUMBER_REGEX, ALLOWED_PHONES
    
    # מציאת כל המספרים
    found_phones = PHONE_NUMBER_REGEX.findall(text)
    
    if found_phones:
        is_all_allowed = True
        for phone in found_phones:
            # בדיקה אם המספר שנמצא (בצורתו המקורית) אינו ברשימה המאושרת
            if phone not in ALLOWED_PHONES:
                is_all_allowed = False
                break
            
        if not is_all_allowed:
            print("⛔️ הודעה מכילה מספר טלפון לא מאושר – לא תועלה לשלוחה.")
            return None, "⛔️ הודעה לא נשלחה: מכילה מספר טלפון לא מאושר."
        
        # --- 🟢 תוספת חדשה: הסרת מספרי טלפון מהטקסט להקראה 🟢 ---
        # אם הגענו לכאן, כל מספרי הטלפון שנמצאו (אם היו) הם מאושרים,
        # ולכן יש להסירם מהטקסט המיועד להקראה (TTS).
        text = PHONE_NUMBER_REGEX.sub('', text)
        print("✅ הודעה מכילה מספרי טלפון, כולם מאושרים. מספרי הטלפון הוסרו מהטקסט המיועד להקראה. ממשיך בסינון.")
        # --- 🟢 סוף תוספת חדשה 🟢 ---


    # --- בדיקה עם רשימות הסינון הנטענות ---
    global STRICT_BANNED, WORD_BANNED, BLOCKED_PHRASES # שימוש ברשימות הגלובליות

    # קבוצה ראשונה – מחפשים בכל מקום (STRICT_BANNED)
    for banned in STRICT_BANNED:
        if banned in text:
            print(f"⛔️ הודעה מכילה מילה אסורה ('{banned}') – לא תועלה לשלוחה.")
            return None, f"⛔️ הודעה לא נשלחה: מכילה מילה אסורה ('{banned}')."

    # קבוצה שנייה – מחפשים רק מילה שלמה (WORD_BANNED)
    words = re.findall(r"\b\w+\b", text)
    for banned in WORD_BANNED:
        if banned in words:
            print(f"⛔️ הודעה מכילה מילה אסורה ('{banned}') – לא תועלה לשלוחה.")
            return None, f"⛔️ הודעה לא נשלחה: מכילה מילה אסורה ('{banned}')."

    # --- ניקוי ביטויים (BLOCKED_PHRASES) ---
    for phrase in BLOCKED_PHRASES:
        text = text.replace(phrase, '')
        
    # --- 🟢🟢🟢 תיקון הקישורים כאן 🟢🟢🟢 ---
    # מחליף את שתי השורות הישנות בשורה אחת חזקה יותר
    # השורות הישנות היו:
    # text = re.sub(r'https?://\S+', '', text)
    # text = re.sub(r'www\.\S+', '', text)
    
    # שורה מתוקנת שמסירה http, https, www, וגם דומיינים כמו example.com
    # זה מונע הקראה של קישורים מאושרים שעברו את הבדיקה
    text = re.sub(r'(?:https?://|www\.)\S+|\b[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', '', text, flags=re.IGNORECASE)
    # --- 🟢🟢🟢 סוף התיקון 🟢🟢🟢 ---

    text = re.sub(r'[^\w\s.,!?()\u0590-\u05FF]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # ✅ הוספת קרדיט אם התחיל ב'חדשות המוקד'
    if add_moked_credit:
        text += ", המוקד"

    return text, None

# ✅ תוספת חדשה: פונקציה להחלת החלפות מילים
def apply_replacements(text, replacements_map):
    """
    מחליף מילים בטקסט לפי מילון, תוך שימוש בגבולות מילה (\b).
    ממיין מפתחות מהארוך לקצר למניעת החלפות חלקיות.
    """
    if not replacements_map:
        return text

    # מיון לפי אורך המפתח, מהארוך לקצר (למשל, כדי ש"ב"ה" יוחלף לפני "ה")
    try:
        sorted_keys = sorted(replacements_map.keys(), key=len, reverse=True)
        
        for key in sorted_keys:
            value = replacements_map[key]
            # שימוש ב-re.escape כדי לטפל בתווים מיוחדים במפתח (כמו נקודות)
            # שימוש ב-\b כדי להבטיח החלפה של מילה שלמה בלבד
            pattern = r'\b' + re.escape(key) + r'\b'
            text = re.sub(pattern, value, text)
            
    except Exception as e:
        print(f"⚠️ שגיאה בהחלת החלפות מילים: {e}")
        # ממשיך עם הטקסט כפי שהוא
    
    return text


def create_full_text(text):
    tz = pytz.timezone('Asia/Jerusalem')
    now = datetime.now(tz)
    hebrew_time = num_to_hebrew_words(now.hour, now.minute)
    return f"{hebrew_time} במבזקים-פלוס. {text}"

def text_to_mp3(text, filename='output.mp3'):
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(
        language_code="he-IL",
        name="he-IL-Wavenet-B",
        ssml_gender=texttospeech.SsmlVoiceGender.MALE
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.2
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )
    with open(filename, "wb") as out:
        out.write(response.audio_content)

def convert_to_wav(input_file, output_file='output.wav'):
    subprocess.run([
        'ffmpeg', '-i', input_file, '-ar', '8000', '-ac', '1', '-f', 'wav',
        output_file, '-y'
    ])

def has_audio_track(file_path):
    """בודק אם יש ערוץ שמע בקובץ וידאו"""
    try:
        result = subprocess.run(
            ['ffprobe', '-i', file_path, '-show_streams', '-select_streams', 'a', '-loglevel', 'error'],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except Exception as e:
        print("⚠️ שגיאה בבדיקת ffprobe:", e)
        return False

# ✅ תוספת: בדיקה אם קובץ WAV מכיל דיבור אנושי
def contains_human_speech(wav_path, frame_duration=30):
    try:
        vad = webrtcvad.Vad(1)
        with wave.open(wav_path, 'rb') as wf:
            # בדיקת פורמט קובץ, אם לא 8k/16k מונו 16bit, המר
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2 or wf.getframerate() not in [8000, 16000]:
                convert_to_wav(wav_path, 'temp.wav')
                wf = wave.open('temp.wav', 'rb')
            frames = wf.readframes(wf.getnframes())
            frame_size = int(wf.getframerate() * frame_duration / 1000) * 2
            speech_detected = False
            for i in range(0, len(frames), frame_size):
                frame = frames[i:i+frame_size]
                if len(frame) < frame_size:
                    break
                if vad.is_speech(frame, wf.getframerate()):
                    speech_detected = True
                    break
            if os.path.exists('temp.wav'):
                os.remove('temp.wav')
            return speech_detected
    except Exception as e:
        print("⚠️ שגיאה בבדיקת דיבור אנושי:", e)
        return False

# ⚠️ הפונקציה עודכנה ללוג מפורט יותר!
def upload_to_ymot(wav_file_path):
    # ✅ ✅ ✅ התיקון הקריטי כאן: הוספנו את הנקודה הדרושה (.co.il)
    url = 'https://call2all.co.il/ym/api/UploadFile' 
    for i in range(5):
        try:
            with open(wav_file_path, 'rb') as f:
                files = {'file': (os.path.basename(wav_file_path), f, 'audio/wav')}
                data = {
                    'token': YMOT_TOKEN,
                    'path': YMOT_PATH,
                    'convertAudio': '1',
                    'autoNumbering': 'true'
                }
                
                response = requests.post(url, data=data, files=files, timeout=60)
                
                # --- ✅ בדיקות לוג חדשות ---
                response.raise_for_status() # זורק שגיאה עבור 4xx/5xx
                
                print(f"📞 תגובת ימות: סטטוס {response.status_code}, תוכן: {response.text}")
                
                # בדיקה אם התוכן מכיל הודעת שגיאה ידועה
                if "error" in response.text.lower() or "שגיאה" in response.text:
                    raise Exception(f"תגובת שגיאה מימות המשיח: {response.text}")
                    
                return response.text
                
        except requests.exceptions.RequestException as req_e:
            # ללכוד שגיאות רשת, timeout, או סטטוס קוד רע (מ-raise_for_status)
            wait_time = 2 ** i + random.uniform(0, 1)
            print(f"⚠️ שגיאה בחיבור או סטטוס (HTTP {getattr(req_e.response, 'status_code', 'N/A')}): {req_e}. ניסיון נוסף בעוד {wait_time:.1f} שניות...")
            time.sleep(wait_time)
        except Exception as e:
            # ללכוד שגיאות אחרות (כמו הודעת שגיאה מפורשת בגוף התגובה)
            wait_time = 2 ** i + random.uniform(0, 1)
            print(f"⚠️ שגיאה בהעלאה ({e}). ניסיון נוסף בעוד {wait_time:.1f} שניות...")
            time.sleep(wait_time)
            
    # אם כל הניסיונות נכשלו
    return "❌ נכשלה העלאה לימות המשיח לאחר מספר ניסיונות."


# ✅ ✅ ✅ פונקציה חדשה – מוקדם יותר בקוד
async def safe_send(bot, chat_id, text, **kwargs):
    """שולח הודעה לטלגרם עם טיפול ב-429"""
    for i in range(5): # עד 5 ניסיונות
        try:
            await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            return
        except Exception as e:
            if "429" in str(e):
                wait_time = 2 ** i + random.uniform(0, 1) # backoff
                print(f"⚠️ נחסמתי זמנית (429). מחכה {wait_time:.1f} שניות...")
                await asyncio.sleep(wait_time)
            else:
                print(f"⚠️ שגיאה בשליחת הודעה לטלגרם: {e}")
                return

# ✅ פונקציה שבודקת אם עכשיו שבת או חג
async def is_shabbat_or_yom_tov():
    try:
        url = "https://www.hebcal.com/zmanim?cfg=json&im=1&geonameid=293397"
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        data = res.json()

        is_assur = data.get("status", {}).get("isAssurBemlacha", False)
        local_time = data.get("status", {}).get("localTime", "לא ידוע")

        print(f"⌛ בדיקת שבת/חג - עכשיו (זמן מקומי): {local_time}")
        print(f"🔍 האם עכשיו אסור במלאכה? {'✅ כן' if is_assur else '❌ לא'}")

        return is_assur
    except Exception as e:
        print(f"⚠️ שגיאה בבדיקת שבת/חג: {e}")
        return False

# ⬇️ ⬇️ עכשיו אפשר להשתמש בה כאן בתוך handle_message ⬇️ ⬇️
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    # ✅ תוספת – עצירה אוטומטית בשבתות וחגים
    if await is_shabbat_or_yom_tov():
        print("📵 שבת/חג – דילוג על ההודעה")
        return

    text = message.text or message.caption
    has_video = message.video is not None
    has_audio = message.audio is not None or message.voice is not None
    
    # ❌ הסרנו את הדגל הישן text_already_uploaded = False

    async def send_error_to_channel(reason):
        if context.bot:
            # שימוש ב-safe_send
            await safe_send(context.bot, message.chat_id, reason) 

    global ALLOWED_LINKS # שימוש ברשימה הגלובלית שנטענה
    if text and any(re.search(r'https?://\S+|www\.\S+', part) for part in text.split()):
        if not any(link in text for link in ALLOWED_LINKS):
            reason = "⛔️ הודעה לא נשלחה: קישור לא מאושר."
            print(reason)
            await send_error_to_channel(reason)
            return
            
    # ✅ ✅ ✅ לוגיקה חדשה: טיפול בטקסט (סינון וכפילות) פעם אחת בלבד
    cleaned_text = None
    if text:
        cleaned, reason = clean_text(text)
        
        if cleaned is None: # נכשל בסינון (מילה אסורה/טלפון לא מאושר)
            if reason:
                await send_error_to_channel(reason)
            return

        if not cleaned: # נכשל בניקוי (טקסט נמחק לחלוטין)
            reason = "⛔️ הודעה לא נשלחה: הטקסט נמחק לחלוטין על ידי פילטר הניקוי."
            print(reason)
            await send_error_to_channel(reason)
            return

        # --- בדיקת כפילות (הדבר שרצית להוסיף) ---
        last_messages = load_last_messages()
        for previous in last_messages:
            similarity = SequenceMatcher(None, cleaned, previous).ratio()
            # 0.55 הוא סף סביר לכפילות, כפי שהוגדר בקוד המקורי שלך
            if similarity >= 0.55:
                reason = f"⏩ הודעה דומה מדי להודעה קודמת ({similarity*100:.1f}%) – לא תועלה לשלוחה."
                print(reason)
                await send_error_to_channel(reason)
                return
        
        # אם עבר את כל הבדיקות, הטקסט מוכן ונוסיף אותו להיסטוריה
        # זה מונע כפילות גם כשיש מדיה וגם כשיש טקסט בלבד
        last_messages.append(cleaned)
        save_last_messages(last_messages)
        
        # ✅ תוספת חדשה: החלת החלפות מילים
        # עושים זאת *אחרי* בדיקת הכפילות, אבל *לפני* השליחה ל-TTS
        global WORD_REPLACEMENTS
        if WORD_REPLACEMENTS:
            print(f"🔍 מחיל {len(WORD_REPLACEMENTS)} החלפות מילים...")
            cleaned_text = apply_replacements(cleaned, WORD_REPLACEMENTS)
        else:
            cleaned_text = cleaned
        # ---------------------------------------------
        
    # 2. טיפול בוידאו (אם יש)
    if has_video:
        video_file = await message.video.get_file()
        await video_file.download_to_drive("video.mp4")

        # 2א. בדיקת שמע בוידאו
        if not has_audio_track("video.mp4"):
            reason = "⛔️ הודעה לא נשלחה: וידאו ללא שמע."
            print(reason)
            await send_error_to_channel(reason)
            os.remove("video.mp4")
            return

        convert_to_wav("video.mp4", "video.wav")

        # 2ב. בדיקת דיבור אנושי
        if not contains_human_speech("video.wav"):
            reason = "⛔️ הודעה לא נשלחה: שמע אינו דיבור אנושי."
            print(reason)
            await send_error_to_channel(reason)
            os.remove("video.mp4")
            os.remove("video.wav")
            return

        # 2ג. יצירת קובץ אודיו סופי לשלוחה
        if cleaned_text: # אם יש טקסט שעבר סינון, כפילות והחלפה, צרף אותו
            print("✅ יוצר שמע מ-TTS (עם החלפות) ומצרף לשמע הוידאו.")
            full_text = create_full_text(cleaned_text)
            text_to_mp3(full_text, "text.mp3")
            convert_to_wav("text.mp3", "text.wav")
            # שרשור TTS + וידאו אודיו
            subprocess.run(['ffmpeg', '-i', 'text.wav', '-i', 'video.wav', '-filter_complex',
                            '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]', 'media.wav', '-y'])
            os.remove("text.mp3")
            os.remove("text.wav")
            os.remove("video.wav")
        else: # אין טקסט/הטקסט היה ריק, השתמש רק בשמע הוידאו
            print("✅ מעלה את שמע הוידאו בלבד.")
            os.rename("video.wav", "media.wav")

        # 2ד. העלאה וניקוי
        upload_to_ymot("media.wav")
        os.remove("video.mp4")
        os.remove("media.wav")

    # 3. טיפול באודיו (אם יש)
    elif has_audio:
        print("✅ מעלה קובץ אודיו/הקלטה קולית.")
        audio_file = await (message.audio or message.voice).get_file()
        await audio_file.download_to_drive("audio.ogg")
        convert_to_wav("audio.ogg", "media.wav")
        upload_to_ymot("media.wav")
        os.remove("audio.ogg")
        os.remove("media.wav")

    # 4. טיפול בטקסט בלבד (אם יש טקסט ואין וידאו/אודיו)
    elif cleaned_text: # אם הגענו לכאן, זה טקסט בלבד שכבר עבר סינון, כפילות, היסטוריה והחלפה
        print("✅ מעלה טקסט (TTS) בלבד (עם החלפות).")
        full_text = create_full_text(cleaned_text)
        text_to_mp3(full_text, "output.mp3")
        convert_to_wav("output.mp3", "output.wav")
        upload_to_ymot("output.wav")
        os.remove("output.mp3")
        os.remove("output.wav")

    # ❌ הקוד המקורי הוסר:
    # if text and not text_already_uploaded: # ✅ לא נשלח פעמיים
    #    cleaned, reason = clean_text(text)
    #    # ... כל לוגיקת הסינון והכפילות שהעברנו למעלה היתה כאן

# 🛠️ פונקציה לבריחת תווים מיוחדים (Markdown V1)
def escape_markdown_v1(text):
    """
    Escapes special characters (*, _, `, [) for Telegram's Markdown V1 parsing 
    to prevent BadRequest errors when displaying user-defined filter items.
    """
    text = str(text) # לוודא שזה סטרינג
    text = text.replace('*', '\\*')
    text = text.replace('_', '\\_')
    text = text.replace('`', '\\`')
    text = text.replace('[', '\\[')
    return text

# 🧑‍💻 פקודת /list_filters: הצגת כל הרשימות
async def list_filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    print(f"DEBUG: נשלחה פקודה /list_filters מ- User ID: {user_id}") # DEBUG PRINT
    
    if not ADMIN_USER_ID: # ✅ בדיקה מפורשת של משתנה סביבה חסר
        await update.message.reply_text("❌ שגיאה: משתנה הסביבה ADMIN_USER_ID אינו מוגדר. לא ניתן לבצע פעולות ניהול.")
        return

    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # טעינה מחדש של הנתונים העדכניים לפני הצגה
    current_data = load_filters()
    if not current_data:
        await update.message.reply_text("⚠️ שגיאה בטעינת קובץ הסינון.")
        return

    response = "📜 *רשימות סינון פעילות* 📜\n\n"
    for friendly_name, json_key in FILTER_MAPPING.items():
        items = current_data.get(json_key, [])
        response += f"*{friendly_name}* (`{json_key}`): ({len(items)} פריטים)\n"
        if items:
            # ✅ בריחת תווים מיוחדים בפריטי הסינון לפני הצגה
            escaped_items = [escape_markdown_v1(item) for item in items[:5]]
            response += "  " + "\n  ".join(escaped_items)
            if len(items) > 5:
                response += f"\n  ... ועוד {len(items) - 5} פריטים."
        response += "\n\n"

    # ✅ הוספת טיפ לפקודה החדשה
    response += "_לצפייה ברשימה מלאה, השתמש ב־_`/view_filter <שם_רשימה>`\n"

    await update.message.reply_text(response, parse_mode="Markdown")

# 🔍 פקודת /view_filter: הצגת כל הפריטים ברשימה ספציפית
async def view_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not ADMIN_USER_ID: 
        await update.message.reply_text("❌ שגיאה: משתנה הסביבה ADMIN_USER_ID אינו מוגדר. לא ניתן לבצע פעולות ניהול.")
        return

    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # מצפה לפורמט: /view_filter <list_name>
    if len(context.args) != 1:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"⚠️ שימוש: /view_filter <{names}>. (הרשימות: {names})")
        return

    list_name = context.args[0]
    if list_name not in FILTER_MAPPING:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"❌ שם רשימה לא קיים. הרשימות הזמינות: {names}")
        return

    json_key = FILTER_MAPPING[list_name]
    current_data = load_filters()
    if not current_data:
        await update.message.reply_text("⚠️ שגיאה בטעינת קובץ הסינון.")
        return

    items = current_data.get(json_key, [])
    
    if not items:
        await update.message.reply_text(f"✅ הרשימה *{list_name}* ריקה.", parse_mode="Markdown")
        return
        
    header = f"📜 *כל הפריטים ברשימה {list_name}* ({len(items)} פריטים):\n\n"
    
    # עוטף את הפריטים במספור ובריחה
    list_content = "\n".join([f"{i+1}. {escape_markdown_v1(item)}" for i, item in enumerate(items)])
    
    full_message = header + list_content

    # פיצול הודעה אם היא ארוכה מדי (מעל 4000 תווים)
    MAX_TELEGRAM_LENGTH = 4000
    if len(full_message) > MAX_TELEGRAM_LENGTH:
        messages = []
        # מתחיל עם הכותרת כדי שכל חלק יהיה קריא
        current_part = header
        
        # פיצול לפי שורות
        for line in list_content.split('\n'):
            # אם הוספת השורה הבאה תגרום לחריגה מהמגבלה
            if len(current_part) + len(line) + 1 > MAX_TELEGRAM_LENGTH:
                messages.append(current_part)
                # מתחיל חלק חדש עם כותרת דומה
                current_part = header.replace("כל הפריטים", "המשך הפריטים") + line
            else:
                current_part += "\n" + line
        messages.append(current_part) # הוספת החלק האחרון

        for msg in messages:
            # שימוש ב-safe_send כדי למנוע חסימה
            await safe_send(context.bot, update.effective_chat.id, msg, parse_mode="Markdown")
            await asyncio.sleep(0.5) # מניעת 429
            
    else:
        await update.message.reply_text(full_message, parse_mode="Markdown")


# ➕ פקודת /add_filter: הוספת פריט
async def add_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    print(f"DEBUG: נשלחה פקודה /add_filter מ- User ID: {user_id}") # DEBUG PRINT
    
    if not ADMIN_USER_ID: # ✅ בדיקה מפורשת של משתנה סביבה חסר
        await update.message.reply_text("❌ שגיאה: משתנה הסביבה ADMIN_USER_ID אינו מוגדר. לא ניתן לבצע פעולות ניהול.")
        return

    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # מצפה לפורמט: /add_filter <list_name> <item>
    if len(context.args) < 2:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"⚠️ שימוש: /add_filter <{names}> <הפריט>")
        return

    list_name = context.args[0]
    item_to_add = " ".join(context.args[1:])

    if list_name not in FILTER_MAPPING:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"❌ שם רשימה לא קיים. הרשימות הזמינות: {names}")
        return

    json_key = FILTER_MAPPING[list_name]
    
    current_data = load_filters()
    if not current_data:
        await update.message.reply_text("⚠️ שגיאה בטעינת קובץ הסינון.")
        return

    # הוספת הפריט
    items = current_data.get(json_key, [])
    if item_to_add in items:
        await update.message.reply_text(f"ℹ️ הפריט '{item_to_add}' כבר קיים ברשימה {list_name}.")
        return

    items.append(item_to_add)
    current_data[json_key] = items

    # שמירה ועדכון גלובלי
    if save_filters(current_data):
        # טעינה מחדש של הגלובליות כדי שהבוט יתחיל להשתמש בהן מיד
        load_filters() 
        # ✅ בריחה בתוך הודעת האישור
        escaped_item = escape_markdown_v1(item_to_add)
        await update.message.reply_text(f"✅ הפריט '{escaped_item}' נוסף לרשימה *{list_name}* בהצלחה!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ שגיאה בשמירת הקובץ. הפריט לא נוסף.")


# ➖ פקודת /remove_filter: הסרת פריט
async def remove_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    print(f"DEBUG: נשלחה פקודה /remove_filter מ- User ID: {user_id}") # DEBUG PRINT
    
    if not ADMIN_USER_ID: # ✅ בדיקה מפורשת של משתנה סביבה חסר
        await update.message.reply_text("❌ שגיאה: משתנה הסביבה ADMIN_USER_ID אינו מוגדר. לא ניתן לבצע פעולות ניהול.")
        return
        
    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # מצפה לפורמט: /remove_filter <list_name> <item>
    if len(context.args) < 2:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"⚠️ שימוש: /remove_filter <{names}> <הפריט>")
        return

    list_name = context.args[0]
    item_to_remove = " ".join(context.args[1:])

    if list_name not in FILTER_MAPPING:
        names = ", ".join(FILTER_MAPPING.keys())
        await update.message.reply_text(f"❌ שם רשימה לא קיים. הרשימות הזמינות: {names}")
        return

    json_key = FILTER_MAPPING[list_name]
    
    current_data = load_filters()
    if not current_data:
        await update.message.reply_text("⚠️ שגיאה בטעינת קובץ הסינון.")
        return

    # הסרת הפריט
    items = current_data.get(json_key, [])
    if item_to_remove not in items:
        await update.message.reply_text(f"ℹ️ הפריט '{item_to_remove}' לא נמצא ברשימה {list_name}.")
        return

    items.remove(item_to_remove)
    current_data[json_key] = items

    # שמירה ועדכון גלובלי
    if save_filters(current_data):
        # טעינה מחדש של הגלובליות כדי שהבוט יתחיל להשתמש בהן מיד
        load_filters() 
        # ✅ בריחה בתוך הודעת האישור
        escaped_item = escape_markdown_v1(item_to_remove)
        await update.message.reply_text(f"✅ הפריט '{escaped_item}' הוסר מהרשימה *{list_name}* בהצלחה!", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ שגיאה בשמירת הקובץ. הפריט לא הוסר.")
    
# --- ✅ תוספת חדשה: פקודות לניהול החלפות מילים ---

# 📜 פקודת /list_replacements: הצגת כל ההחלפות
async def list_replacements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # טעינה מחדש של הנתונים העדכניים
    current_data = load_replacements()
    
    if not current_data:
        await update.message.reply_text("ℹ️ רשימת החלפות המילים ריקה.")
        return

    response = "📜 *רשימת החלפות מילים פעילות* 📜\n"
    response += "הבוט יחליף (כמילה שלמה) את הקיצור בצד ימין במילה המלאה בצד שמאל:\n\n"
    
    try:
        # מיון לפי מפתח (הקיצור)
        sorted_items = sorted(current_data.items())
        
        for key, value in sorted_items:
            response += f"`{escape_markdown_v1(key)}` ⬅️ `{escape_markdown_v1(value)}`\n"

        if len(response) > 4000:
             await update.message.reply_text(response[:4000] + "\n... (הרשימה ארוכה מדי להצגה מלאה)")
        else:
             await update.message.reply_text(response, parse_mode="Markdown")
            
    except Exception as e:
        await update.message.reply_text(f"⚠️ שגיאה ביצירת הרשימה: {e}")

# ➕ פקודת /add_replacement: הוספה או עדכון החלפה
async def add_replacement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # מצפה לפורמט: /add_replacement <מילה-לחיפוש> <מילה-להחלפה>
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ שימוש: /add_replacement <קיצור> <החלפה מלאה>\nלדוגמה: `/add_replacement ה השם`", parse_mode="Markdown")
        return

    key = context.args[0]
    value = " ".join(context.args[1:])

    current_data = load_replacements()
    current_data[key] = value

    if save_replacements(current_data):
        # אין צורך בטעינה מחדש, save_replacements מעדכן את המשתנה הגלובלי
        escaped_key = escape_markdown_v1(key)
        escaped_value = escape_markdown_v1(value)
        await update.message.reply_text(f"✅ החלפה נוספה/עודכנה:\n`{escaped_key}` ⬅️ `{escaped_value}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ שגיאה בשמירת קובץ ההחלפות.")

# ➖ פקודת /remove_replacement: הסרת החלפה
async def remove_replacement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ אין לך הרשאה לבצע פעולה זו.")
        return

    # מצפה לפורמט: /remove_replacement <מילה-לחיפוש>
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ שימוש: /remove_replacement <קיצור>\nלדוגמה: `/remove_replacement ה`", parse_mode="Markdown")
        return

    key = context.args[0]

    current_data = load_replacements()
    
    if key not in current_data:
        await update.message.reply_text(f"ℹ️ הקיצור `{escape_markdown_v1(key)}` לא נמצא ברשימת ההחלפות.", parse_mode="Markdown")
        return

    # שמירת הערך שהוסר להצגה
    removed_value = current_data.pop(key)

    if save_replacements(current_data):
        escaped_key = escape_markdown_v1(key)
        escaped_value = escape_markdown_v1(removed_value)
        await update.message.reply_text(f"✅ החלפה הוסרה:\n`{escaped_key}` (היה ⬅️ `{escaped_value}`)", parse_mode="Markdown")
    else:
        # אם השמירה נכשלה, נחזיר את הערך כדי למנוע חוסר עקביות
        current_data[key] = removed_value
        await update.message.reply_text("❌ שגיאה בשמירת הקובץ. ההסרה בוטלה.")

# --- סוף תוספת חדשה ---
    
# ♻️ keep alive
from keep_alive import keep_alive
keep_alive()

# ▶️ הפעלת הבוט
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_message))

# ✅ הוספת CommandHandler לניהול הפילטרים בצ'אט פרטי עם האדמין
app.add_handler(CommandHandler("list_filters", list_filters_command, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("add_filter", add_filter_command, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("remove_filter", remove_filter_command, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("view_filter", view_filter_command, filters=filters.ChatType.PRIVATE))

# ✅ תוספת חדשה: הוספת CommandHandler לניהול החלפות מילים
app.add_handler(CommandHandler("list_replacements", list_replacements_command, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("add_replacement", add_replacement_command, filters=filters.ChatType.PRIVATE))
app.add_handler(CommandHandler("remove_replacement", remove_replacement_command, filters=filters.ChatType.PRIVATE))


print("🚀 הבוט מאזין לערוץ ומעלה לשלוחה 🎧")

import telegram
telegram.Bot(BOT_TOKEN).delete_webhook()

# ▶️ לולאת הרצה אינסופית
while True:
    try:
        app.run_polling(
            poll_interval=10.0,      # כל כמה שניות לבדוק הודעות חדשות
            timeout=30,              # כמה זמן לחכות לפני שנזרקת שגיאת TimedOut
            allowed_updates=Update.ALL_TYPES # לוודא שכל סוגי ההודעות נתפסים
        )
    except Exception as e:
        print("❌ שגיאה כללית בהרצת הבוט:", e)
        time.sleep(30) # לחכות 30 שניות ואז להפעיל מחדש את הבוט
