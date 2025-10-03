import logging
import os
import io
import datetime
import pytz
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackContext,
    CallbackQueryHandler
)
from google.cloud import texttospeech, speech
from firebase_admin import credentials, firestore as admin_firestore, initialize_app
from pydub import AudioSegment
from geopy.geocoders import Nominatim
from typing import Tuple

# --- Configuration ---
AudioSegment.converter = r"C:\ffmpeg\bin\ffmpeg.exe"
TELEGRAM_TOKEN = "8415632607:AAE2H9jIDYX1zQkm7whRjVsBkwN1dpf4Gj8"
FIRESTORE_PROJECT_ID = "handy-outpost-471016-c5"

FASTAPI_BASE_URL = "http://127.0.0.1:8000"

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-credentials.json"

# --- Initialization ---
try:
    cred = credentials.ApplicationDefault()
    initialize_app(cred, {'projectId': FIRESTORE_PROJECT_ID})
    db = admin_firestore.client()
except Exception as e:
    print(f"Could not initialize Firebase Admin SDK: {e}")
    db = None

speech_client = speech.SpeechClient()
tts_client = texttospeech.TextToSpeechClient()
geolocator = Nominatim(user_agent="telegram-questionnaire-bot")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Definitions for ConversationHandler ---
(
    SELECTING_LANG, ASKING_QUESTION,
    ASKING_LOCATION, CONFIRMING_LOCATION,
    SELECTING_ACTIVITY, LOGGING_NOTE, CONFIRM_LOG_AGAIN,
    MAIN_MENU, AWAITING_PEST_PHOTO, AWAITING_QUERY
) = range(10)

# --- Bot Data ---
QUESTIONS = {
    'en': ["What is your name?", "What is your age?", "What is the area of your land in acres?", "What are the main crops you grow?"],
    'ml': ["നിങ്ങളുടെ പേരെന്താണ്?", "നിങ്ങൾക്ക് എത്ര വയസ്സായി?", "നിങ്ങളുടെ ഭൂമിയുടെ വിസ്തീർണ്ണം ഏക്കറിൽ എത്രയാണ്?", "നിങ്ങൾ കൃഷി ചെയ്യുന്ന പ്രധാന വിളകൾ ഏതൊക്കെയാണ്?"]
}

BUTTON_LABELS = {
    'en': {
        'irrigation': "Irrigation 💧", 'fertilizing': "Fertilizing 🌱",
        'pest_control': "Pest Control 🐞", 'weeding': "Weeding 🌿",
        'sowing': "Sowing / Planting 🌾", 'harvesting': "Harvesting 🧺",
        'other': "Other 🗒️", 'ask_queries': "Ask queries",
        'analyze_leaf': "Analyze Leaf Image 📸", 'ask_general': "Ask a General Question ❓",
        'return_logging': "Return to Logging 📝", 'finish_session': "Finish Session 👋",
        'yes': "Yes", 'no': "No"
    },
    'ml': {
        'irrigation': "ജലസേചനം 💧", 'fertilizing': "വളപ്രയോഗം 🌱",
        'pest_control': "കീടനിയന്ത്രണം 🐞", 'weeding': "കളനിയന്ത്രണം 🌿",
        'sowing': "വിതയ്ക്കൽ / നടീൽ 🌾", 'harvesting': "വിളവെടുപ്പ് 🧺",
        'other': "മറ്റുള്ളവ 🗒️", 'ask_queries': "ചോദ്യങ്ങൾ ചോദിക്കുക",
        'analyze_leaf': "ഇലയുടെ ചിത്രം വിശകലനം ചെയ്യുക 📸",
        'ask_general': "ഒരു പൊതു ചോദ്യം ചോദിക്കുക ❓",
        'return_logging': "ലോഗിംഗിലേക്ക് മടങ്ങുക 📝",
        'finish_session': "സെഷൻ അവസാനിപ്പിക്കുക 👋",
        'yes': "അതെ", 'no': "ഇല്ല"
    }
}

MESSAGES = {
    'en': {
        'ask_location': "Thank you. To find your location, please tap the button below.",
        'got_location': "Got it! Looking up your address...",
        'location_confirm_text': "I see you are near:\n📍 {full_address}\n\nIs this correct?",
        'location_confirm_speech': "You are near {full_address}. Is this correct?",
        'location_confirmed': "Location confirmed: {full_address}",
        'saving_details': "Thank you! Saving your details...",
        'details_saved_intro': (
            "Thank you! Your details have been saved.\n\n"
            "Here’s how you can log your daily activities:\n"
            "1. Right Now: We will start your first logging session immediately.\n"
            "2. Manually: You can start logging anytime by sending the /log command.\n"
            "3. Daily Reminders:  We will send you a reminder every evening at 7 PM.\n\n"
            "Now, let's log your first activity for today.\n\n\n"
            "Manual Commands (Anytime):\n"
            "You can also click the following commands at any time:\n"
            "• /log - Start logging a new farm activity.\n"
            "• /pest - Analyze a plant leaf photo for disease.\n"
            "• /ask - Ask any agricultural question.\n\n"
        ),
        'details_saved_speech': "Your details are saved. I will now explain how to use the bot.",
        'activity_selected': "You selected: {activity}. Please describe this activity in text or voice message.",
        'ask_agri_question': "What is your agricultural question? You can send it as text or a voice message.",
        'transcribing_voice': "Transcribing your voice message...",
        'thinking': "Thinking... I'm looking up an answer for you. 🤔",
        'analysis_error': "Sorry, I couldn't process your query. Reason: {error}",
        'connection_error': "I couldn't connect to the analysis server. Please make sure the backend service is running.",
    },
    'ml': {
        'ask_location': "നന്ദി. നിങ്ങളുടെ ലൊക്കേഷൻ കണ്ടെത്താൻ, ദയവായി താഴെയുള്ള ബട്ടൺ ടാപ്പ് ചെയ്യുക.",
        'got_location': "ലൊക്കേഷൻ ലഭിച്ചു! നിങ്ങളുടെ വിലാസം കണ്ടെത്തുന്നു...",
        'location_confirm_text': "നിങ്ങൾ ഇവിടെയാണ്:\n📍 {full_address}\n\nഇത് ശരിയാണോ?",
        'location_confirm_speech': "നിങ്ങൾ {full_address} എന്ന സ്ഥലത്താണ്. ഇത് ശരിയാണോ?",
        'location_confirmed': "സ്ഥലം സ്ഥിരീകരിച്ചു: {full_address}",
        'saving_details': "നന്ദി! നിങ്ങളുടെ വിവരങ്ങൾ സേവ് ചെയ്യുന്നു...",
        'details_saved_intro': (
            "നന്ദി! നിങ്ങളുടെ വിവരങ്ങൾ സേവ് ചെയ്തിരിക്കുന്നു.\n\n"
            "നിങ്ങളുടെ ദൈനംദിന പ്രവർത്തനങ്ങൾ എങ്ങനെ രേഖപ്പെടുത്താമെന്ന് താഴെക്കൊടുക്കുന്നു:\n"
            "1. ഇപ്പോൾത്തന്നെ: നിങ്ങളുടെ ആദ്യത്തെ ലോഗിംഗ് സെഷൻ ഞങ്ങൾ ഉടൻ ആരംഭിക്കും.\n"
            "2. സ്വമേധയാ: /log കമാൻഡ് ഉപയോഗിച്ച് നിങ്ങൾക്ക് എപ്പോൾ വേണമെങ്കിലും ലോഗിംഗ് ആരംഭിക്കാം.\n"
            "3. ദിവസേനയുള്ള ഓർമ്മപ്പെടുത്തലുകൾ: എല്ലാ ദിവസവും വൈകുന്നേരം 7 മണിക്ക് ഞങ്ങൾ നിങ്ങളെ ഓർമ്മിപ്പിക്കും.\n\n"
            "ഇനി, ഇന്നത്തെ നിങ്ങളുടെ ആദ്യത്തെ പ്രവർത്തനം രേഖപ്പെടുത്താം.\n\n\n"
            "മാനുവൽ കമാൻഡുകൾ (എപ്പോൾ വേണമെങ്കിലും):\n"
            "നിങ്ങൾക്ക് എപ്പോൾ വേണമെങ്കിലും ഈ കമാൻഡുകളിൽ ക്ലിക്ക് ചെയ്യാം:\n"
            "• /log - ഒരു പുതിയ കാർഷിക പ്രവർത്തനം രേഖപ്പെടുത്താൻ.\n"
            "• /pest - സസ്യ ഇലയുടെ ഫോട്ടോ രോഗനിർണ്ണയത്തിനായി വിശകലനം ചെയ്യാൻ.\n"
            "• /ask - കാർഷികപരമായ ഏത് ചോദ്യവും ചോദിക്കാൻ.\n\n"
        ),
        'details_saved_speech': "നിങ്ങളുടെ വിവരങ്ങൾ സേവ് ചെയ്തിരിക്കുന്നു. ബോട്ട് എങ്ങനെ ഉപയോഗിക്കണമെന്ന് ഞാൻ ഇപ്പോൾ വിശദീകരിക്കാം.",
        'activity_selected': "നിങ്ങൾ തിരഞ്ഞെടുത്തത്: {activity}. ദയവായി ഈ പ്രവർത്തനം ടെക്സ്റ്റ് അല്ലെങ്കിൽ വോയിസ് സന്ദേശമായി വിവരിക്കുക.",
        'ask_agri_question': "നിങ്ങളുടെ കാർഷിക സംബന്ധമായ ചോദ്യം എന്താണ്? നിങ്ങൾക്ക് ടെക്സ്റ്റ് ആയോ വോയിസ് മെസ്സേജ് ആയോ അയക്കാം.",
        'transcribing_voice': "നിങ്ങളുടെ ശബ്ദ സന്ദേശം ട്രാൻസ്ക്രൈബ് ചെയ്യുന്നു...",
        'thinking': "ആലോചിക്കുന്നു... നിങ്ങൾക്കായി ഒരു ഉത്തരം കണ്ടെത്തുന്നു. 🤔",
        'analysis_error': "ക്ഷമിക്കണം, നിങ്ങളുടെ ചോദ്യം പ്രോസസ്സ് ചെയ്യാൻ കഴിഞ്ഞില്ല. കാരണം: {error}",
        'connection_error': "എനിക്ക് അനാലിസിസ് സെർവറുമായി ബന്ധപ്പെടാൻ കഴിഞ്ഞില്ല. ദയവായി ബാക്കെൻഡ് സേവനം പ്രവർത്തിക്കുന്നുണ്ടോയെന്ന് ഉറപ്പുവരുത്തുക.",
    }
}

VOICE_CONFIGS = {
    'en': texttospeech.VoiceSelectionParams(language_code="en-US", ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL),
    'ml': texttospeech.VoiceSelectionParams(language_code="ml-IN", name="ml-IN-Wavenet-C")
}

# --- Helper Functions ---
async def text_to_speech_and_send(context: CallbackContext, chat_id: int, text: str, lang: str):
    audio_content = await text_to_speech(text, lang)
    if audio_content:
        await context.bot.send_voice(chat_id=chat_id, voice=audio_content)

async def text_to_speech(text: str, lang_code: str) -> bytes:
    try:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.OGG_OPUS)
        voice_params = VOICE_CONFIGS.get(lang_code, VOICE_CONFIGS['en'])
        response = tts_client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_config)
        return response.audio_content
    except Exception as e:
        logger.error(f"Text-to-speech error: {e}")
        return b""

async def speech_to_text(audio_data: bytes, lang_code: str) -> str:
    audio = speech.RecognitionAudio(content=audio_data)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        sample_rate_hertz=48000,
        language_code=lang_code
    )
    try:
        response = speech_client.recognize(config=config, audio=audio)
        return response.results[0].alternatives[0].transcript if response.results else ""
    except Exception as e:
        logger.error(f"Speech-to-text error: {e}")
    return ""

async def get_address_from_coords(latitude: float, longitude: float) -> dict:
    try:
        location = geolocator.reverse((latitude, longitude), exactly_one=True, language='en')
        if location and location.raw.get('address'):
            address = location.raw['address']
            district = address.get('county', address.get('state_district', 'Unknown'))
            return {"full_address": location.address, "district": district}
        return {"full_address": "Address not found.", "district": "Unknown"}
    except Exception as e:
        logger.error(f"Geocoding error: {e}")
        return {"full_address": "Could not fetch address.", "district": "Error"}

async def get_user_lang(context: CallbackContext, user_id: int) -> str:
    if 'lang' in context.user_data:
        return context.user_data['lang']
    if db:
        user_doc = db.collection('questionnaire_responses').document(str(user_id)).get()
        if user_doc.exists:
            lang = user_doc.to_dict().get('language', 'en')
            context.user_data['lang'] = lang
            return lang
    return 'en'

async def register_user_on_backend(user_id: int, user_data: dict):
    url = f"{FASTAPI_BASE_URL}/register"
    try:
        answers = user_data.get('answers', {})
        age = int(answers.get(QUESTIONS['en'][1], 0)) if answers.get(QUESTIONS['en'][1]) else 0
        payload = {
            "user_name": str(user_id), "age": age,
            "district": user_data.get("district", "Unknown"),
            "crops_grown": answers.get(QUESTIONS['en'][3], "Unknown"),
            "farm_size": answers.get(QUESTIONS['en'][2], "0")
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    logger.info(f"Successfully registered user {user_id} on the backend.")
                    return True
                else:
                    logger.error(f"Failed to register user {user_id}. Status: {response.status}, Response: {await response.text()}")
                    return False
    except Exception as e:
        logger.error(f"Exception during backend registration: {e}")
        return False

async def analyze_pest_image_on_backend(user_id: int, photo_data: bytes, lang_code: str) -> dict:
    url = f"{FASTAPI_BASE_URL}/analyze"
    data = aiohttp.FormData()
    data.add_field('user_name', str(user_id))
    data.add_field('lang_code', lang_code)
    data.add_field('file', photo_data, filename='photo.jpg', content_type='image/jpeg')
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as response:
                return await response.json() if response.status == 200 else {"success": False, "error": f"API Error: {response.status}"}
    except aiohttp.ClientConnectorError:
        logger.error("Backend Connection Error during image analysis.")
        return {"success": False, "error": "Connection Error"}
    except Exception as e:
        logger.error(f"An unexpected error occurred during image analysis: {e}")
        return {"success": False, "error": f"An unexpected error occurred: {e}"}

async def ask_query_on_backend(user_id: int, query: str, lang_code: str) -> dict:
    url = f"{FASTAPI_BASE_URL}/text_query"
    payload = {"user_name": str(user_id), "query_text": query, "lang_code": lang_code}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return await response.json() if response.status == 200 else {"success": False, "error": f"API Error: {response.status}"}
    except aiohttp.ClientConnectorError:
        logger.error("Backend Connection Error during text query.")
        return {"success": False, "error": "Connection Error"}
    except Exception as e:
        logger.error(f"An unexpected error occurred during text query: {e}")
        return {"success": False, "error": f"An unexpected error occurred: {e}"}

# --- Questionnaire Flow ---
async def start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    text = "Welcome! Please choose your language. Say 'English' or 'മലയാളം'."
    await update.message.reply_text(text)
    await text_to_speech_and_send(context, update.effective_chat.id, text, 'en')
    return SELECTING_LANG

async def select_language(update: Update, context: CallbackContext) -> int:
    user_input = ""
    if update.message.text:
        user_input = update.message.text.lower()
    elif update.message.voice:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_data = await voice_file.download_as_bytearray()
        audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
        processed_voice_data = audio.export(format="ogg", codec="libopus").read()
        transcript_en = await speech_to_text(processed_voice_data, 'en-US')
        transcript_ml = await speech_to_text(processed_voice_data, 'ml-IN')
        user_input = (transcript_en + " " + transcript_ml).lower()

    if "english" in user_input:
        context.user_data['lang'] = 'en'
        await update.message.reply_text("You have selected English.")
    elif "മലയാളം" in user_input or "malayalam" in user_input:
        context.user_data['lang'] = 'ml'
        await update.message.reply_text("നിങ്ങൾ മലയാളം തിരഞ്ഞെടുത്തു.")
    else:
        await update.message.reply_text("Sorry, I didn't understand. Please say 'English' or 'മലയാളം'.")
        return SELECTING_LANG

    context.user_data['question_index'] = 0
    context.user_data['answers'] = {}
    await ask_next_question(update, context)
    return ASKING_QUESTION

async def ask_next_question(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    lang = context.user_data['lang']
    q_index = context.user_data.get('question_index', 0)
    question_text = QUESTIONS[lang][q_index]
    await context.bot.send_message(chat_id=chat_id, text=question_text)
    await text_to_speech_and_send(context, chat_id, question_text, lang)

async def handle_answer(update: Update, context: CallbackContext) -> int:
    lang = context.user_data.get('lang', 'en')
    q_index = context.user_data.get('question_index', 0)
    
    question_text_en = QUESTIONS['en'][q_index]
    answer_text = ""
    
    if update.message.text:
        answer_text = update.message.text
    elif update.message.voice:
        lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_data = await voice_file.download_as_bytearray()
        audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
        processed_voice_data = audio.export(format="ogg", codec="libopus").read()
        answer_text = await speech_to_text(processed_voice_data, lang_code=lang_code)

    if not answer_text:
        fail_text = "Sorry, I couldn't understand. Please try again."
        await update.message.reply_text(fail_text)
        await text_to_speech_and_send(context, update.effective_chat.id, fail_text, lang)
        return ASKING_QUESTION

    context.user_data['answers'][question_text_en] = answer_text
    
    # --- FIXED: Linear questionnaire flow ---
    if q_index == 0: # After name, ask age
        context.user_data['question_index'] += 1
        await ask_next_question(update, context)
        return ASKING_QUESTION
    elif q_index == 1: # After age, ask location
        return await ask_for_location(update, context)
    elif q_index == 2: # After acres, ask crops
        context.user_data['question_index'] += 1
        await ask_next_question(update, context)
        return ASKING_QUESTION
    elif q_index == 3: # After crops, finish
        return await save_questionnaire_and_start_logging(update, context)

    # Fallback in case of unexpected index
    return ConversationHandler.END


async def ask_for_location(update: Update, context: CallbackContext) -> int:
    chat_id = update.effective_chat.id
    lang = context.user_data.get('lang', 'en')
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text="Location incorrect. Let's try again.")
        
    location_button = KeyboardButton(text="📍 Share My Location", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button]], resize_keyboard=True, one_time_keyboard=True)
    text = MESSAGES[lang]['ask_location']

    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    await text_to_speech_and_send(context, chat_id, text, lang)
    return ASKING_LOCATION

async def handle_location(update: Update, context: CallbackContext) -> int:
    if not update.message.location:
        await update.message.reply_text("Please use the button to send your location.")
        return ASKING_LOCATION
    lang = context.user_data.get('lang', 'en')
    await update.message.reply_text(MESSAGES[lang]['got_location'], reply_markup=ReplyKeyboardRemove())
    address_info = await get_address_from_coords(update.message.location.latitude, update.message.location.longitude)
    context.user_data['full_address'] = address_info['full_address']
    context.user_data['district'] = address_info['district']
    text_for_display = MESSAGES[lang]['location_confirm_text'].format(full_address=address_info['full_address'])
    text_for_speech = MESSAGES[lang]['location_confirm_speech'].format(full_address=address_info['full_address'])
    labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
    keyboard = [[
        InlineKeyboardButton(labels['yes'], callback_data='loc_confirm_yes'),
        InlineKeyboardButton(labels['no'], callback_data='loc_confirm_no')
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text_for_display, reply_markup=reply_markup)
    await text_to_speech_and_send(context, update.effective_chat.id, text_for_speech, lang)
    return CONFIRMING_LOCATION

async def handle_location_confirmation(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get('lang', 'en')

    if query.data == 'loc_confirm_yes':
        context.user_data['answers']['Fetched Address'] = context.user_data.get('full_address', 'N/A')
        text = MESSAGES[lang]['location_confirmed'].format(full_address=context.user_data['full_address'])
        await query.edit_message_text(text=text)

        # FIXED: Explicitly move to the next question (acres)
        context.user_data['question_index'] = 2 
        await ask_next_question(update, context)
        return ASKING_QUESTION
            
    elif query.data == 'loc_confirm_no':
        await query.edit_message_text(text="Okay, let's try getting your location again.")
        return await ask_for_location(update, context)
        
    return CONFIRMING_LOCATION


async def save_questionnaire_and_start_logging(update: Update, context: CallbackContext) -> int:
    user = update.effective_user
    chat_id = update.effective_chat.id
    lang = context.user_data.get('lang', 'en')
    
    await context.bot.send_message(chat_id=chat_id, text=MESSAGES[lang]['saving_details'])
    if db:
        user_doc_ref = db.collection('questionnaire_responses').document(str(user.id))
        data_to_save = {
            'user_id': user.id, 'username': user.username, 'language': lang,
            'responses': context.user_data.get('answers', {}),
            'timestamp': admin_firestore.SERVER_TIMESTAMP
        }
        user_doc_ref.set(data_to_save)
    await register_user_on_backend(user.id, context.user_data)
    
    text = MESSAGES[lang]['details_saved_intro']
    speech_text = MESSAGES[lang]['details_saved_speech']
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
    await text_to_speech_and_send(context, chat_id, speech_text, lang)
    
    return await prompt_for_activity(update, context)

# --- Activity Logging Flow ---
async def log_start(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    context.user_data['lang'] = await get_user_lang(context, user_id)
    await update.message.reply_text("Starting a new logging session...")
    return await prompt_for_activity(update, context)

async def prompt_for_activity(update: Update, context: CallbackContext) -> int:
    lang = context.user_data.get('lang', 'en')
    labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
    keyboard = [
        [InlineKeyboardButton(labels['irrigation'], callback_data='Irrigation'), InlineKeyboardButton(labels['fertilizing'], callback_data='Fertilizing')],
        [InlineKeyboardButton(labels['pest_control'], callback_data='Pest Control'), InlineKeyboardButton(labels['weeding'], callback_data='Weeding')],
        [InlineKeyboardButton(labels['sowing'], callback_data='Sowing'), InlineKeyboardButton(labels['harvesting'], callback_data='Harvesting')],
        [InlineKeyboardButton(labels['ask_queries'], callback_data='jump_to_ai'), InlineKeyboardButton(labels['other'], callback_data='Other')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "📝 Please choose the activity you want to log, or ask the queries."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup)
    await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
    return SELECTING_ACTIVITY

async def log_activity_button(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'jump_to_ai':
        context.user_data['came_from_logging'] = True
        return await show_main_menu(update, context)
    context.user_data['current_activity'] = query.data
    lang = context.user_data.get('lang', 'en')
    text = MESSAGES[lang]['activity_selected'].format(activity=query.data)
    await query.edit_message_text(text=text)
    await text_to_speech_and_send(context, query.message.chat_id, text, lang)
    return LOGGING_NOTE

async def log_note(update: Update, context: CallbackContext) -> int:
    note_text = ""
    lang = context.user_data.get('lang', 'en')
    if update.message.text:
        note_text = update.message.text
    elif update.message.voice:
        lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_data = await voice_file.download_as_bytearray()
        audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
        processed_voice_data = audio.export(format="ogg", codec="libopus").read()
        note_text = await speech_to_text(processed_voice_data, lang_code=lang_code)
    if not note_text:
        await update.message.reply_text("Sorry, I couldn't understand. Please try sending the note again.")
        return LOGGING_NOTE
    try:
        user = update.message.from_user
        log_data = {
            'activity_type': context.user_data.get('current_activity'),
            'note': note_text, 'timestamp': admin_firestore.SERVER_TIMESTAMP
        }
        if db:
            db.collection('questionnaire_responses').document(str(user.id)).collection('activity_logs').add(log_data)
        labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
        keyboard = [[
            InlineKeyboardButton(labels['yes'], callback_data='log_again_yes'),
            InlineKeyboardButton(labels['no'], callback_data='log_again_no')
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        confirm_text = "Your activity has been logged! Would you like to log another one?"
        await update.message.reply_text(confirm_text, reply_markup=reply_markup)
        await text_to_speech_and_send(context, update.effective_chat.id, confirm_text, lang)
        return CONFIRM_LOG_AGAIN
    except Exception as e:
        logger.error(f"Error saving log: {e}")
        await update.message.reply_text("Sorry, there was an error saving your log.")
        return ConversationHandler.END

async def log_again_or_end(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == 'log_again_yes':
        return await prompt_for_activity(update, context)
    else:
        context.user_data['just_finished_logging'] = True
        return await show_main_menu(update, context)

# --- AI Assistant Menu and Flows ---
async def show_main_menu(update: Update, context: CallbackContext) -> int:
    lang = await get_user_lang(context, update.effective_user.id)
    labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
    keyboard = [
        [InlineKeyboardButton(labels['analyze_leaf'], callback_data='ask_pest')],
        [InlineKeyboardButton(labels['ask_general'], callback_data='ask_general')]
    ]
    
    keyboard.append([InlineKeyboardButton(labels['return_logging'], callback_data='jump_back_to_log')])
    
    keyboard.append([InlineKeyboardButton(labels['finish_session'], callback_data='end_session')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if context.user_data.pop('just_finished_logging', False):
         text = "Your activities are logged. What would you like to do next?"
    else:
        text = "What would you like to do next?"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup)
    await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
    return MAIN_MENU

async def main_menu_handler(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == 'jump_back_to_log':
        context.user_data.pop('came_from_logging', None)
        return await prompt_for_activity(update, context)
    elif query.data == 'ask_pest':
        return await pest_start(update, context)
    elif query.data == 'ask_general':
        return await ask_start(update, context)
    elif query.data == 'end_session':
        return await end_session(update, context)
    return MAIN_MENU

async def pest_start(update: Update, context: CallbackContext) -> int:
    lang = await get_user_lang(context, update.effective_user.id)
    text = " To analyze for pests or diseases, please send me a clear photo of the affected plant leaf."
    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
    return AWAITING_PEST_PHOTO

async def handle_pest_photo(update: Update, context: CallbackContext) -> int:
    lang = await get_user_lang(context, update.effective_user.id)
    await update.message.reply_text("Analyzing the image... This may take a moment. 🔬")
    photo_file = await update.message.photo[-1].get_file()
    photo_data = await photo_file.download_as_bytearray()
    result = await analyze_pest_image_on_backend(update.effective_user.id, photo_data, lang)
    if result and result.get("success"):
        description = result.get('description', 'No description provided.')
        diagnosis = result.get('diagnosis', 'No diagnosis provided.')
        response_text = f"--- Image Description ---\n{description}\n\n--- Diagnosis & Recommendations ---\n{diagnosis}"
        await update.message.reply_text(response_text)
        await text_to_speech_and_send(context, update.effective_chat.id, diagnosis, lang)
    else:
        error_code = result.get("error", "An unknown error occurred.")
        if error_code == "Connection Error":
            error_message = MESSAGES[lang]['connection_error']
        else:
            error_message = MESSAGES[lang]['analysis_error'].format(error=error_code)
        
        await update.message.reply_text(error_message)
        await text_to_speech_and_send(context, update.effective_chat.id, error_message, lang)
    return await show_main_menu(update, context)

async def ask_start(update: Update, context: CallbackContext) -> int:
    lang = await get_user_lang(context, update.effective_user.id)
    text = MESSAGES[lang]['ask_agri_question']
    if update.callback_query:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)
    await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
    return AWAITING_QUERY

async def handle_query(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    lang = await get_user_lang(context, user_id)
    query_text = ""
    if update.message.text:
        query_text = update.message.text
    elif update.message.voice:
        await update.message.reply_text(MESSAGES[lang]['transcribing_voice'])
        lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_data = await voice_file.download_as_bytearray()
        audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
        processed_voice_data = audio.export(format="ogg", codec="libopus").read()
        query_text = await speech_to_text(processed_voice_data, lang_code=lang_code)
    if not query_text:
        await update.message.reply_text("I'm sorry, I couldn't understand your query. Please try again.")
        return AWAITING_QUERY
    await update.message.reply_text(MESSAGES[lang]['thinking'])
    result = await ask_query_on_backend(user_id, query_text, lang)
    if result and result.get("success"):
        response_text = result.get('response', 'I could not find an answer.')
        await update.message.reply_text(response_text)
        await text_to_speech_and_send(context, update.effective_chat.id, response_text, lang)
    else:
        error_code = result.get("error", "An unknown error occurred.")
        if error_code == "Connection Error":
            error_message = MESSAGES[lang]['connection_error']
        else:
            error_message = MESSAGES[lang]['analysis_error'].format(error=error_code)

        await update.message.reply_text(error_message)
        await text_to_speech_and_send(context, update.effective_chat.id, error_message, lang)
    return await show_main_menu(update, context)

# --- General Handlers ---
async def end_session(update: Update, context: CallbackContext) -> int:
    text = "Great! Have a productive day! 👋"
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text)
    else:
        await update.message.reply_text(text)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text('Operation cancelled.')
    context.user_data.clear()
    return ConversationHandler.END

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('log', log_start),
            CommandHandler('pest', pest_start),
            CommandHandler('ask', ask_start),
        ],
        states={
            SELECTING_LANG: [MessageHandler(filters.TEXT | filters.VOICE, select_language)],
            ASKING_QUESTION: [MessageHandler(filters.TEXT | filters.VOICE, handle_answer)],
            ASKING_LOCATION: [MessageHandler(filters.LOCATION, handle_location)],
            CONFIRMING_LOCATION: [CallbackQueryHandler(handle_location_confirmation)],
            SELECTING_ACTIVITY: [CallbackQueryHandler(log_activity_button)],
            LOGGING_NOTE: [MessageHandler(filters.TEXT | filters.VOICE & ~filters.COMMAND, log_note)],
            CONFIRM_LOG_AGAIN: [CallbackQueryHandler(log_again_or_end)],
            MAIN_MENU: [CallbackQueryHandler(main_menu_handler)],
            AWAITING_PEST_PHOTO: [MessageHandler(filters.PHOTO, handle_pest_photo)],
            AWAITING_QUERY: [MessageHandler(filters.TEXT | filters.VOICE, handle_query)],
        },
        fallbacks=[CommandHandler('cancel', cancel), CommandHandler('end', end_session)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()