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
MAX_HISTORY = 16

# 📁 הגדרות ומשתנים קריטיים ל-AI וזמנים
# 🆕 הנחיות מערכת קשוחות ל-AI: סינון דתי, פרסומות וניקוי קרדיטים ארוכים
SYSTEM_PROMPT = """You are a content filtering and editing engine for a strictly Haredi (Ultra-Orthodox Jewish) news broadcast platform.

Your primary goal is to assess content sensitivity, filter out prohibited topics, and remove editorial spam/credits, while retaining essential news information.

RULES:
1. REJECTION: If the content contains advertisements, promotional material, profane/immodest language, or discussions of sports, secular entertainment (TV, music, movies), celebrities, or political controversy (unless it is a neutral news report), output ONLY the exact, single word: 'REJECTED'.
2. CLEANUP: If the content is approved, output ONLY the cleaned version of the text. Remove all long/unnecessary sign-offs, credits (names, phone numbers, links), and editorial fluff. The output must be concise and ready for immediate speech synthesis.

Output format MUST be strictly either the cleaned text OR 'REJECTED'."""

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"

# 🆕 ביטוי רגולרי לזיהוי זמנים בפורמט HH:MM או HH.MM בטקסט (לצורך SSML)
TIME_REGEX = re.compile(r'(\b\d{1,2}[.:]\d{2}\b)')

# 📁 קובץ הגדרות סינון (הרשימות הגלובליות נשארות, אך לא בשימוש ישיר)
FILTERS_FILE = "filters.json"
BLOCKED_PHRASES = []
STRICT_BANNED = []
WORD_BANNED = []
ALLOWED_LINKS = []
ALLOWED_PHONES = [] 

# ✅ חדש: מיפוי שמות פשוטים למפתחות JSON (נשאר לטובת load/save)
FILTER_MAPPING = {
    "ניקוי": "BLOCKED_PHRASES",
    "איסור-חזק": "STRICT_BANNED",
    "איסור-מילה": "WORD_BANNED",
    "קישורים": "ALLOWED_LINKS",
    "מספרים-מאושרים": "ALLOWED_PHONES"
}
# ---------------------------------------------
# פונקציות load/save נשארות כפי שהן
# ---------------------------------------------

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

# ⚙️ פונקציה לטעינת הגדרות הסינון (נשארת לטובת עבודה יציבה)
def load_filters():
    global BLOCKED_PHRASES, STRICT_BANNED, WORD_BANNED, ALLOWED_LINKS, ALLOWED_PHONES
    
    default_data = {
        "BLOCKED_PHRASES": [], "STRICT_BANNED": [], "WORD_BANNED": [],
        "ALLOWED_LINKS": [], "ALLOWED_PHONES": []
    }

    if not os.path.exists(FILTERS_FILE):
        with open(FILTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_data, f, ensure_ascii=False, indent=4)
        return default_data

    try:
        with open(FILTERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        BLOCKED_PHRASES = sorted(data.get("BLOCKED_PHRASES", default_data["BLOCKED_PHRASES"]), key=len, reverse=True)
        STRICT_BANNED = data.get("STRICT_BANNED", default_data["STRICT_BANNED"])
        WORD_BANNED = data.get("WORD_BANNED", default_data["WORD_BANNED"])
        ALLOWED_LINKS = data.get("ALLOWED_LINKS", default_data["ALLOWED_LINKS"])
        ALLOWED_PHONES = data.get("ALLOWED_PHONES", default_data["ALLOWED_PHONES"])

        print(f"✅ נטעו נתוני הגדרות (לא משמשים לסינון הראשי, אלא לניהול).")
        return data
    except Exception as e:
        print(f"❌ נכשל בטעינת קובץ הגדרות סינון: {e}")
        return None

def save_filters(data):
    try:
        filtered_data = {k: data.get(k, []) for k in FILTER_MAPPING.values()}
        with open(FILTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(filtered_data, f, ensure_ascii=False, indent=4)
        print("✅ הגדרות הסינון נשמרו בהצלחה.")
        return True
    except Exception as e:
        print(f"❌ שגיאה בשמירת הגדרות סינון: {e}")
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
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")

# טוען את הפילטרים מיד לאחר הגדרת המשתנים הגלובליים
try:
    filter_data = load_filters()
except Exception as e:
    print(e)
    pass

# 🔒 פונקציה לבדיקת הרשאת אדמין
def is_admin(user_id):
    if not ADMIN_USER_ID:
        return False
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

# 🆕 פונקציה שמחליפה ייצוגי זמן מספריים בתגי SSML
def replace_times_with_ssml(text):
    """
    מחליף ייצוגי זמן (HH:MM או HH.MM) בטקסט בתגי SSML כדי להבטיח קריאה נכונה בעברית.
    """
    global TIME_REGEX
    def replace_match(match):
        # מוודא שהפורמט הוא H:M ע"י החלפת נקודה בנקודתיים
        time_str = match.group(1).replace('.', ':') 
        # שימוש בתג SSML להורות למנוע ה-TTS לקרוא כזמן
        return f'<say-as interpret-as="time">{time_str}</say-as>'

    # מחפש תבנית זמן בטקסט ומחליף אותה בייצוג SSML
    return TIME_REGEX.sub(replace_match, text)


# 🟢 🟢 🟢 הפונקציה המרכזית החדשה לסינון וניקוי באמצעות AI 🟢 🟢 🟢
async def ai_filter_and_clean(text):
    """
    שולח את הטקסט למודל Gemini לסינון (דחייה/אישור) וניקוי (הסרת קרדיטים/פרסומות).
    """
    if not text.strip():
        return None, "⛔️ הודעה ריקה."
        
    try:
        # יצירת מטען נתונים ל-API
        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        }
        
        # שימוש ב-asyncio.to_thread להפעלת requests.post באופן לא חוסם
        response = await asyncio.to_thread(
            requests.post,
            f"{GEMINI_API_URL}?key={os.getenv('GEMINI_API_KEY')}",
            headers={'Content-Type': 'application/json'},
            json=payload,
            timeout=20 # זמן המתנה סביר לתגובה מ-AI
        )
        response.raise_for_status() # זורק חריגה לקודי סטטוס שגויים
        
        result = response.json()
        
        # בדיקה של התוצאה
        cleaned_text = result['candidates'][0]['content']['parts'][0]['text'].strip()

    except Exception as e:
        print(f"❌ שגיאה חריגה בבדיקת AI: {e}")
        # אם יש שגיאה ב-AI, נפסול את ההודעה כדי להימנע מתוכן לא מסונן
        return None, f"⚠️ שגיאה בבדיקת AI. לא ניתן לאשר את ההודעה." 
        
    # בדיקה אם ה-AI החליט לפסול
    if cleaned_text == 'REJECTED':
        print("⛔️ AI פסל את ההודעה (תוכן אסור/פרסומת).")
        return None, "⛔️ הודעה נפסלה על ידי מסנן AI (תוכן לא תואם/פרסומת)."

    # בדיקה אחרונה: אם ה-AI ניקה את ההודעה כולה
    if not cleaned_text:
        return None, "⛔️ הודעה נפסלה: הטקסט נמחק לחלוטין על ידי AI."

    # ה-AI אמור להחזיר רק את הטקסט הנקי, כולל הקרדיט המינימלי
    return cleaned_text, None
# 🟢 🟢 🟢 סוף פונקציית AI 🟢 🟢 🟢


# ⚠️ הפונקציה clean_text הפכה למעטפת אסינכרונית כדי להתאים ל-handle_message
async def clean_text(text):
    # הפונקציה הישנה clean_text מוחלפת בקריאה ל-AI
    return await ai_filter_and_clean(text)
    
def create_full_text(text):
    tz = pytz.timezone('Asia/Jerusalem')
    now = datetime.now(tz)
    hebrew_time = num_to_hebrew_words(now.hour, now.minute)
    return f"{hebrew_time} במבזקים-פלוס. {text}"

def text_to_mp3(text, filename='output.mp3'):
    client = texttospeech.TextToSpeechClient()
    
    # 🆕 אם הטקסט מכיל תגי SSML, יש לעטוף אותו בתג <speak> ולהשתמש ב-ssml=
    if '<say-as' in text:
        synthesis_input = texttospeech.SynthesisInput(ssml=f"<speak>{text}</speak>")
    else:
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
async def safe_send(bot, chat_id, text):
    """שולח הודעה לטלגרם עם טיפול ב-429"""
    for i in range(5): # עד 5 ניסיונות
        try:
            await bot.send_message(chat_id=chat_id, text=text)
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

    text_already_uploaded = False # ✅ דגל חדש

    async def send_error_to_channel(reason):
        if context.bot:
            # שימוש ב-safe_send
            await safe_send(context.bot, message.chat_id, reason) 

    # 🛑 🛑 הוסרו בדיקות הקישורים הישנות (ALLOWED_LINKS) כיוון שהן מוחלפות על ידי AI 🛑 🛑
    # if text and any(re.search(r'https?://\S+|www\.\S+', part) for part in text.split()):
    #     if not any(link in text for link in ALLOWED_LINKS):
    #         reason = "⛔️ הודעה לא נשלחה: קישור לא מאושר."
    #         print(reason)
    #         await send_error_to_channel(reason)
    #         return


    if has_video:
        video_file = await message.video.get_file()
        await video_file.download_to_drive("video.mp4")

        if not has_audio_track("video.mp4"):
            reason = "⛔️ הודעה לא נשלחה: וידאו ללא שמע."
            print(reason)
            await send_error_to_channel(reason)
            os.remove("video.mp4")
            return

        convert_to_wav("video.mp4", "video.wav")

        if not contains_human_speech("video.wav"):
            reason = "⛔️ הודעה לא נשלחה: שמע אינו דיבור אנושי."
            print(reason)
            await send_error_to_channel(reason)
            os.remove("video.mp4")
            os.remove("video.wav")
            return

        if text:
            cleaned, reason_text = await clean_text(text) # ⚠️ הפונקציה כעת אסינכרונית
            if cleaned is None:
                if reason_text:
                    await send_error_to_channel(reason_text)
                os.remove("video.mp4")
                os.remove("video.wav")
                return
            
            # 🆕 שלב 1: החלפת זמנים מספריים בתגי SSML על הטקסט הנקי מה-AI
            text_with_ssml_times = replace_times_with_ssml(cleaned)
            
            full_text = create_full_text(text_with_ssml_times)
            text_to_mp3(full_text, "text.mp3")
            convert_to_wav("text.mp3", "text.wav")
            subprocess.run(['ffmpeg', '-i', 'text.wav', '-i', 'video.wav', '-filter_complex',
                             '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]', 'media.wav', '-y'])
            os.remove("text.mp3")
            os.remove("text.wav")
            os.remove("video.wav")
            text_already_uploaded = True # ✅ טקסט כבר נשלח
        else:
            os.rename("video.wav", "media.wav")

        upload_to_ymot("media.wav")
        os.remove("video.mp4")
        os.remove("media.wav")

    elif has_audio:
        audio_file = await (message.audio or message.voice).get_file()
        await audio_file.download_to_drive("audio.ogg")
        convert_to_wav("audio.ogg", "media.wav")
        upload_to_ymot("media.wav")
        os.remove("audio.ogg")
        os.remove("media.wav")

    if text and not text_already_uploaded: # ✅ לא נשלח פעמיים
        cleaned, reason = await clean_text(text) # ⚠️ הפונקציה כעת אסינכרונית
        if cleaned is None:
            if reason:
                await send_error_to_channel(reason)
            return

        last_messages = load_last_messages()
        for previous in last_messages:
            similarity = SequenceMatcher(None, cleaned, previous).ratio()
            if similarity >= 0.55:
                reason = f"⏩ הודעה דומה מדי להודעה קודמת ({similarity*100:.1f}%) – לא תועלה לשלוחה."
                print(reason)
                await send_error_to_channel(reason)
                return
        last_messages.append(cleaned)
        save_last_messages(last_messages)

        # 🆕 שלב 1: החלפת זמנים מספריים בתגי SSML
        text_with_ssml_times = replace_times_with_ssml(cleaned)

        # 🆕 שלב 2: יצירת הטקסט המלא (כולל הכותרת) עם הזמנים המעובדים
        full_text = create_full_text(text_with_ssml_times)

        # 🆕 שלב 3: הפונקציה text_to_mp3 תטפל כעת ב-SSML
        text_to_mp3(full_text, "output.mp3")
        convert_to_wav("output.mp3", "output.wav")
        upload_to_ymot("output.wav")
        os.remove("output.mp3")
        os.remove("output.wav")

# 🛠️ פונקציה לבריחת תווים מיוחדים (Markdown V1)
def escape_markdown_v1(text):
    """
    Escapes special characters (*, _, `, [) for Telegram's Markdown V1 parsing 
    to prevent BadRequest errors when displaying user-defined filter items.
    """
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
            await safe_send(context.bot, update.effective_chat.id, msg)
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

print("🚀 הבוט מאזין לערוץ ומעלה לשלוחה 🎧")

import telegram
telegram.Bot(BOT_TOKEN).delete_webhook()

# ▶️ לולאת הרצה אינסופית
while True:
    try:
        app.run_polling(
            poll_interval=10.0,    # כל כמה שניות לבדוק הודעות חדשות
            timeout=30,            # כמה זמן לחכות לפני שנזרקת שגיאת TimedOut
            allowed_updates=Update.ALL_TYPES # לוודא שכל סוגי ההודעות נתפסים
        )
    except Exception as e:
        print("❌ שגיאה כללית בהרצת הבוט:", e)
        time.sleep(30) # לחכות 5 שניות ואז להפעיל מחדש את הבוט
