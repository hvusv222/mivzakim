import os
import json
import subprocess
import requests
import base64
from datetime import datetime
import pytz
import asyncio
import re
from difflib import SequenceMatcher
import wave
import webrtcvad
import time
import random
import logging

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters
from google.cloud import texttospeech

# 🟢 מזהה המנהל
ADMIN_ID = 7820835795

# 🔧 הגדרת לוגים
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# 🟣 קובץ הסינון
FILTERS_FILE = "filters_config.json"
DEFAULT_FILTERS = {
    "STRICT_BANNED": ["טיקטוק", "OnlyFans", "פורנו"],
    "WORD_BANNED": ["חזה", "מחשוף", "נשיקה"]
}

def load_filters():
    if os.path.exists(FILTERS_FILE):
        try:
            with open(FILTERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.warning("שגיאת JSON — נטען מילים ברירת מחדל")
            return DEFAULT_FILTERS
    else:
        save_filters(DEFAULT_FILTERS)
        return DEFAULT_FILTERS

def save_filters(filters_data):
    with open(FILTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(filters_data, f, ensure_ascii=False, indent=2)

# 🔹 פונקציית ניקוי טקסט (מחזירה cleaned, reason)
def clean_text(text, filters_data):
    for word in filters_data["STRICT_BANNED"]:
        if word in text:
            return None, f"⛔️ הודעה לא נשלחה: מכילה מילה אסורה ('{word}')."
    for word in filters_data["WORD_BANNED"]:
        text = text.replace(word, "*" * len(word))

    # ניקוי ביטויים נוספים
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    text = re.sub(r'[^\w\s.,!?()\u0590-\u05FF]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    return text, None

# 🔹 קובץ היסטוריית הודעות
LAST_MESSAGES_FILE = "last_messages.json"
MAX_HISTORY = 16

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

# 🟡 כתיבת קובץ Google JSON מ־BASE64
key_b64 = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_B64")
if not key_b64:
    raise Exception("❌ משתנה GOOGLE_APPLICATION_CREDENTIALS_B64 לא מוגדר או ריק")

with open("google_key.json", "wb") as f:
    f.write(base64.b64decode(key_b64))
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google_key.json"

# 🔢 המרת מספרים לעברית
def num_to_hebrew_words(hour, minute):
    hours_map = {
        1: "אחת", 2: "שתיים", 3: "שלוש", 4: "ארבע", 5: "חמש",
        6: "שש", 7: "שבע", 8: "שמונה", 9: "תשע", 10: "עשר",
        11: "אחת עשרה", 12: "שתים עשרה"
    }
    minutes_map = {0:"",15:"ורבע",30:"וחצי"}
    return f"{hours_map[hour%12 or 12]} {minutes_map.get(minute,'')}"

def create_full_text(text):
    tz = pytz.timezone('Asia/Jerusalem')
    now = datetime.now(tz)
    hebrew_time = num_to_hebrew_words(now.hour, now.minute)
    return f"{hebrew_time} במבזקים-פלוס. {text}"

# 🔹 המרת טקסט ל-MP3
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
    subprocess.run(['ffmpeg', '-i', input_file, '-ar', '8000', '-ac', '1', '-f', 'wav', output_file, '-y'])

def has_audio_track(file_path):
    try:
        result = subprocess.run(
            ['ffprobe','-i',file_path,'-show_streams','-select_streams','a','-loglevel','error'],
            capture_output=True, text=True
        )
        return bool(result.stdout.strip())
    except:
        return False

def contains_human_speech(wav_path, frame_duration=30):
    try:
        vad = webrtcvad.Vad(1)
        with wave.open(wav_path, 'rb') as wf:
            if wf.getnchannels()!=1 or wf.getsampwidth()!=2 or wf.getframerate() not in [8000,16000]:
                convert_to_wav(wav_path,'temp.wav')
                wf = wave.open('temp.wav','rb')
            frames = wf.readframes(wf.getnframes())
            frame_size = int(wf.getframerate()*frame_duration/1000)*2
            speech_detected = False
            for i in range(0,len(frames),frame_size):
                frame = frames[i:i+frame_size]
                if len(frame)<frame_size: break
                if vad.is_speech(frame,wf.getframerate()):
                    speech_detected=True
                    break
            if os.path.exists('temp.wav'): os.remove('temp.wav')
            return speech_detected
    except:
        return False

# 🔹 העלאה ל-י מוט
YMOT_TOKEN = os.getenv("YMOT_TOKEN")
YMOT_PATH = os.getenv("YMOT_PATH","ivr2:90/")

def upload_to_ymot(wav_file_path):
    url='https://call2all.co.il/ym/api/UploadFile'
    for i in range(5):
        try:
            with open(wav_file_path,'rb') as f:
                files={'file':(os.path.basename(wav_file_path),f,'audio/wav')}
                data={'token':YMOT_TOKEN,'path':YMOT_PATH,'convertAudio':'1','autoNumbering':'true'}
                response=requests.post(url,data=data,files=files,timeout=60)
            print("📞 תגובת ימות:",response.text)
            return response.text
        except Exception as e:
            wait_time=2**i+random.random()
            print(f"⚠️ שגיאה בהעלאה ({e}) – ניסיון נוסף בעוד {wait_time:.1f} שניות")
            time.sleep(wait_time)

# 🔹 שליחה בטוחה לטלגרם
async def safe_send(bot, chat_id, text):
    for i in range(5):
        try:
            await bot.send_message(chat_id=chat_id,text=text)
            return
        except Exception as e:
            if "429" in str(e):
                wait_time=2**i+random.random()
                await asyncio.sleep(wait_time)
            else:
                return

# 🔹 בדיקת שבת/חג
async def is_shabbat_or_yom_tov():
    try:
        url="https://www.hebcal.com/zmanim?cfg=json&im=1&geonameid=293397"
        res = await asyncio.to_thread(requests.get,url,timeout=10)
        data = res.json()
        return data.get("status",{}).get("isAssurBemlacha",False)
    except:
        return False

# 🔹 טיפול בהודעות
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message: return

    if await is_shabbat_or_yom_tov(): return

    text = message.text or message.caption
    has_video = message.video is not None
    has_audio = message.audio is not None or message.voice is not None
    text_already_uploaded = False

    async def send_error(reason):
        if context.bot:
            await safe_send(context.bot,message.chat_id,reason)

    filters_data = load_filters()
    if text:
        cleaned, reason = clean_text(text,filters_data)
        if cleaned is None:
            if reason: await send_error(reason)
            return

    # 🎬 וידאו
    if has_video:
        video_file = await message.video.get_file()
        await video_file.download_to_drive("video.mp4")

        if not has_audio_track("video.mp4"):
            await send_error("⛔️ הודעה לא נשלחה: וידאו ללא שמע.")
            os.remove("video.mp4")
            return

        convert_to_wav("video.mp4","video.wav")
        if not contains_human_speech("video.wav"):
            await send_error("⛔️ הודעה לא נשלחה: שמע אינו דיבור אנושי.")
            os.remove("video.mp4")
            os.remove("video.wav")
            return

        if text:
            full_text = create_full_text(cleaned)
            text_to_mp3(full_text,"text.mp3")
            convert_to_wav("text.mp3","text.wav")
            subprocess.run(['ffmpeg','-i','text.wav','-i','video.wav','-filter_complex',
                            '[0:a][1:a]concat=n=2:v=0:a=1[out]','-map','[out]','media.wav','-y'])
            os.remove("text.mp3"); os.remove("text.wav"); os.remove("video.wav")
            text_already_uploaded=True
        else:
            os.rename("video.wav","media.wav")

        upload_to_ymot("media.wav")
        os.remove("video.mp4"); os.remove("media.wav")

    # 🎵 אודיו בלבד
    elif has_audio:
        audio_file = await (message.audio or message.voice).get_file()
        await audio_file.download_to_drive("audio.ogg")
        convert_to_wav("audio.ogg","media.wav")
        upload_to_ymot("media.wav")
        os.remove("audio.ogg"); os.remove("media.wav")

    # 📝 טקסט בלבד
    if text and not text_already_uploaded:
        last_messages = load_last_messages()
        for prev in last_messages:
            if SequenceMatcher(None,cleaned,prev).ratio()>=0.55:
                await send_error(f"⏩ הודעה דומה מדי להודעה קודמת – לא תועלה")
                return
        last_messages.append(cleaned)
        save_last_messages(last_messages)

        full_text = create_full_text(cleaned)
        text_to_mp3(full_text,"output.mp3")
        convert_to_wav("output.mp3","output.wav")
        upload_to_ymot("output.wav")
        os.remove("output.mp3"); os.remove("output.wav")

# ♻️ keep alive
from keep_alive import keep_alive
keep_alive()

# ▶️ הפעלת הבוט
BOT_TOKEN = os.getenv("BOT_TOKEN")
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_message))

print("🚀 הבוט מאזין לערוץ ומעלה לשלוחה 🎧")

# ▶️ Polling
import telegram
telegram.Bot(BOT_TOKEN).delete_webhook()

while True:
    try:
        app.run_polling(poll_interval=10.0,timeout=30,allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print("❌ שגיאה כללית:",e)
        time.sleep(30)
