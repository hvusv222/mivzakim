import os 
import json
import subprocess
import requests
import base64
from datetime import datetime
import pytz
import asyncio
import re
from difflib import SequenceMatcher  # ✅ חדש
import wave
import webrtcvad  # ✅ תוספת

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.cloud import texttospeech

# 📁 קובץ לשמירת היסטוריית הודעות
LAST_MESSAGES_FILE = "last_messages.json"
MAX_HISTORY = 15  # ✅ שונה מ־10 ל־15

def load_last_messages():
    if not os.path.exists(LAST_MESSAGES_FILE):
        return []
    try:
        with open(LAST_MESSAGES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_last_messages(messages):
    messages = messages[-MAX_HISTORY:]
    with open(LAST_MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False)

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

# 🛠 משתנים מ־Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
YMOT_TOKEN = os.getenv("YMOT_TOKEN")
YMOT_PATH = os.getenv("YMOT_PATH", "ivr2:90/")

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
    BLOCKED_PHRASES = sorted([
        "חדשות המוקד • בטלגרם: t.me/hamoked_il", "בוואטסאפ: https://chat.whatsapp.com/LoxVwdYOKOAH2y2kaO8GQ7",
        "דסק העולם הערבי", "לשיתוף", "לכל העדכונים ~ ראשוני", "סקופים", "צאפ מגזין", "בוואטצאפ", "מצטרפים בקישור", "דסק החוץ", "מבזקן 12", "אסף רוזנצווייג", "אלי הירשמן", "אלעד שמחיוף",
        "איתמר מינמר", "צפו", "כדי להגיב לכתבה לחצו כאן", "ללא צנזורה חדשות ישראל", "לקבוצת הוואטסאפ לעדכונים חריגים", "https://chat.whatsapp.com/B5sAtMyYFlCJCX0eR99g1M", "ברק רביד", "דפנה ליאל", "ענבר טויזר", "אלמוג בוקר", "בWhatsApp", "אסף רוזנצוייג", "ביטחון שוטף", "טלגרם", "ניצן שפירא", "דין פישר", "יעל יפה",
        "ראש דסק 12", "שושי תחוקה", "לכל העדכונים:", "מה שמעניין", "בוואטסאפ ובטלגרם", "אדר גיציס", "צילום", "יובל שדה", "קרן בצלאל", "דביר ג'ברה", "ספיר ליפקין", "ידיעות בני ברק", "להצטרפות", "ישיב’ע זוכע’ר בגוגל צ’אט", "קישור לדיוח אנונימי למערכת", "לכל העדכונים", "נועם כהן", "המַקְרן - רק וידאו", "להצטרפות", "הכי חם ברשת - ’הערינג’", "וואטצפ", "לשליחת חומרים", "053-315-3081", "סקופים מעולם הישיבות הליטאי", "יולן כהן", "תומר אלמגור",
        "לעדכוני הפרגוד בטלגרם", "t.me/hamoked_il", "r0527120704@gmail.com", "בטלגרם", "חדשות המוקד",
        "@New_security8200", "חדשות 8200 בטלגרם", "@N12chat", "מבזקן 12", "כל העדכונים בקבוצה",
        "כל העדכונים בקבוצה:", "לשליחת חומר:", "בוואצפ: 0526356326", "במייל",
        "לכל העדכונים, ולכתבות נוספות הצטרפו לערוץ דרך הקישור",
        "https://t.me/yediyot_bnei_brak", "להצטרפות מלאה לקבוצה לחצו על הצטרף"
    ], key=len, reverse=True)

    BANNED_PHRASES = [
        "באח הגדול", "להטב", "שתפו והצטרפו לקהילת העדכונים", "באונס", "בגבר", "אליפות", "רוכב", "כדורגל", "כדורסל", "ספורט", "ליגה", 
        "אולימפיאדה", "מונדיאל", "זמרת", "סדרה", "קולנוע", "תיאטרון", "נטפליקס", "יוטיוב", "פורנוגרפיה", "מיניות", "קיום יחסים", "אור רביד", "נועם כהן", "אלימות", "תיעוד", "אונס", "ספוטיפיי", "דוגמנית", "הפלות", "זנות", "חשפנות", "סקס", "אהבה", 
        "בגידה", "רומן", "חברה", "זוגיות", "דוגמנית", "ביקיני", "הלבשה תחתונה", "גופייה", "חשוף", "עירום", "פעוט", "ליגת", "פגיעות", "צניעות", "מעשים מגונים", "תועבה", "ליאור באקאלו", "הגאווה", "האח הגדול", "אנס", "שחקנית", "להטבים", "להט\"ב", "להטב״ים", "להטביים",
        "פסטיבל", "עבירות", "קטינה", "גבר", "אירוויזיון", "אישה", "אשה בת", "קטינות", "בן גולדפריינד", "בקטינה", "מינית", "מיניות", "מעשה מגונה"
    ]

    for banned in BANNED_PHRASES:
        if banned in text:
            print(f"⛔️ הודעה מכילה מילה אסורה ('{banned}') – לא תועלה לשלוחה.")
            return None, f"⛔️ הודעה לא נשלחה: מכילה מילה אסורה ('{banned}')."

    for phrase in BLOCKED_PHRASES:
        text = text.replace(phrase, '')
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    text = re.sub(r'[^\w\s.,!?()\u0590-\u05FF]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text, None

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

def upload_to_ymot(wav_file_path):
    url = 'https://call2all.co.il/ym/api/UploadFile'
    with open(wav_file_path, 'rb') as f:
        files = {'file': (os.path.basename(wav_file_path), f, 'audio/wav')}
        data = {
            'token': YMOT_TOKEN,
            'path': YMOT_PATH,
            'convertAudio': '1',
            'autoNumbering': 'true'
        }
        response = requests.post(url, data=data, files=files)
    print("📞 תגובת ימות:", response.text)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    text = message.text or message.caption
    has_video = message.video is not None
    has_audio = message.audio is not None or message.voice is not None

    text_already_uploaded = False   # ✅ דגל חדש

    async def send_error_to_channel(reason):
        if context.bot:
            await context.bot.send_message(chat_id=message.chat_id, text=reason)

    ALLOWED_LINKS = [
        "t.me/hamoked_il",
        "https://t.me/yediyot_bnei_brak",
        "https://chat.whatsapp.com/HRLme3RLzJX0WlaT1Fx9ol",
        "https://chat.whatsapp.com/J9gT1RNxAtMBzwqksTZXCJ",
        "https://chat.whatsapp.com/B5sAtMyYFlCJCX0eR99g1M",
        "https://forms.gle/Pnc2FmAZuHvXXwPD7",
        "https://t.me/News_il_h",
        "https://chat.whatsapp.com/EGTE1vTzkVKGdj3YXUSs5I",
        "https://chat.whatsapp.com/Ca6SOTOwzvY8dBcx78f3cA?mode=ems_share_c",
        "https://t.me/GbmMDm",
        "https://chat.whatsapp.com/IXZNWCRmFUl13WkNucOlby?mode=ac_t",
        "https://bit.ly/YeshivaGroup",
        "r0527120704@gmail.com",
        "https://chat.whatsapp.com/LoxVwdYOKOAH2y2kaO8GQ7"
    ]
    if text and any(re.search(r'https?://\S+|www\.\S+', part) for part in text.split()):
        if not any(link in text for link in ALLOWED_LINKS):
            reason = "⛔️ הודעה לא נשלחה: קישור לא מאושר."
            print(reason)
            await send_error_to_channel(reason)
            return

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
            cleaned, reason_text = clean_text(text)
            if cleaned is None:
                if reason_text:
                    await send_error_to_channel(reason_text)
                os.remove("video.mp4")
                os.remove("video.wav")
                return
            full_text = create_full_text(cleaned)
            text_to_mp3(full_text, "text.mp3")
            convert_to_wav("text.mp3", "text.wav")
            subprocess.run(['ffmpeg', '-i', 'text.wav', '-i', 'video.wav', '-filter_complex',
                            '[0:a][1:a]concat=n=2:v=0:a=1[out]', '-map', '[out]', 'media.wav', '-y'])
            os.remove("text.mp3")
            os.remove("text.wav")
            os.remove("video.wav")
            text_already_uploaded = True   # ✅ טקסט כבר נשלח
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

    if text and not text_already_uploaded:   # ✅ לא נשלח פעמיים
        cleaned, reason = clean_text(text)
        if cleaned is None:
            if reason:
                await send_error_to_channel(reason)
            return

        last_messages = load_last_messages()
        for previous in last_messages:
            similarity = SequenceMatcher(None, cleaned, previous).ratio()
            if similarity >= 0.7:
                reason = f"⏩ הודעה דומה מדי להודעה קודמת ({similarity*100:.1f}%) – לא תועלה לשלוחה."
                print(reason)
                await send_error_to_channel(reason)
                return
        last_messages.append(cleaned)
        save_last_messages(last_messages)

        full_text = create_full_text(cleaned)
        text_to_mp3(full_text, "output.mp3")
        convert_to_wav("output.mp3", "output.wav")
        upload_to_ymot("output.wav")
        os.remove("output.mp3")
        os.remove("output.wav")

# ♻️ keep alive
from keep_alive import keep_alive
keep_alive()

# ▶️ הפעלת האפליקציה
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_message))

print("🚀 הבוט מאזין לערוץ ומעלה לשלוחה 🎧")
app.run_polling()
