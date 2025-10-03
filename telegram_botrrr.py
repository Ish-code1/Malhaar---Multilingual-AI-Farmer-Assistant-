import logging
import os
import io
import datetime
import pytz
import aiohttp

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
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
    except Exception as e:
        return {"success": False, "error": f"Connection Error: {e}"}

async def ask_query_on_backend(user_id: int, query: str, lang_code: str) -> dict:
    url = f"{FASTAPI_BASE_URL}/text_query"
    payload = {"user_name": str(user_id), "query_text": query, "lang_code": lang_code}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                return await response.json() if response.status == 200 else {"success": False, "error": f"API Error: {response.status}"}
    except Exception as e:
        return {"success": False, "error": f"Connection Error: {e}"}

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
    message = update.effective_message
    chat_id = update.effective_chat.id
    lang = context.user_data['lang']
    q_index = context.user_data.get('question_index', 0)
    question_text = QUESTIONS[lang][q_index]
    await message.reply_text(question_text)
    await text_to_speech_and_send(context, chat_id, question_text, lang)

async def handle_answer(update: Update, context: CallbackContext) -> int:
    lang = context.user_data['lang']
    q_index = context.user_data['question_index']
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

    if answer_text:
        context.user_data['answers'][question_text_en] = answer_text
        if q_index == 1:
            return await ask_for_location(update, context)
        context.user_data['question_index'] += 1
        if context.user_data['question_index'] < len(QUESTIONS[lang]):
            await ask_next_question(update, context)
            return ASKING_QUESTION
        else:
            return await save_questionnaire_and_start_logging(update, context)
    else:
        fail_text = "Sorry, I couldn't understand. Please try again."
        await update.message.reply_text(fail_text)
        await text_to_speech_and_send(context, update.effective_chat.id, fail_text, lang)
        return ASKING_QUESTION

async def ask_for_location(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    chat_id = update.effective_chat.id
    if update.callback_query:
        await update.callback_query.edit_message_text(text="Location incorrect. Let's try again.")
    lang = context.user_data.get('lang', 'en')
    location_button = KeyboardButton(text="📍 Share My Location", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button]], resize_keyboard=True, one_time_keyboard=True)
    text = "Thank you. To find your location, please tap the button below."
    await message.reply_text(text, reply_markup=reply_markup)
    await text_to_speech_and_send(context, chat_id, text, lang)
    return ASKING_LOCATION

async def handle_location(update: Update, context: CallbackContext) -> int:
    if not update.message.location:
        await update.message.reply_text("Please use the button to send your location.")
        return ASKING_LOCATION
    lang = context.user_data.get('lang', 'en')
    await update.message.reply_text("Got it! Looking up your address...")
    address_info = await get_address_from_coords(update.message.location.latitude, update.message.location.longitude)
    context.user_data['full_address'] = address_info['full_address']
    context.user_data['district'] = address_info['district']
    text_for_display = f"I see you are near:\n📍 {address_info['full_address']}\n\nIs this correct?"
    text_for_speech = f"You are near {address_info['full_address']}. Is this correct?"
    labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
    keyboard = [[
        InlineKeyboardButton(labels['yes'], callback_data='loc_confirm_yes'),
        InlineKeyboardButton(labels['no'], callback_data='loc_confirm_no')
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text_for_display, reply_markup=reply_markup)
    await text_to_speech_and_send(context, update.effective_chat.id, text_for_speech, lang)
    return CONFIRMING_LOCATION

#BUTTON FOR CONFIRMATION

async def handle_location_confirmation(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    lang = context.user_data.get('lang', 'en')
    if query.data == 'loc_confirm_yes':
        context.user_data['answers']['Fetched Address'] = context.user_data.get('full_address', 'N/A')
        await query.edit_message_text(text=f"Location confirmed: {context.user_data['full_address']}")
        context.user_data['question_index'] += 1
        if context.user_data['question_index'] < len(QUESTIONS[lang]):
            await ask_next_question(update, context)
            return ASKING_QUESTION
        else:
            return await save_questionnaire_and_start_logging(update, context)
    elif query.data == 'loc_confirm_no':
        return await ask_for_location(update, context)
    return CONFIRMING_LOCATION


async def save_questionnaire_and_start_logging(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    user = update.effective_user
    lang = context.user_data.get('lang', 'en')
    await message.reply_text("Thank you! Saving your details...")
    if db:
        user_doc_ref = db.collection('questionnaire_responses').document(str(user.id))
        data_to_save = {
            'user_id': user.id, 'username': user.username, 'language': lang,
            'responses': context.user_data.get('answers', {}),
            'timestamp': admin_firestore.SERVER_TIMESTAMP
        }
        user_doc_ref.set(data_to_save)
    await register_user_on_backend(user.id, context.user_data)
    text = (
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
        
    )
    await message.reply_text(text, parse_mode='Markdown')
    await text_to_speech_and_send(context, update.effective_chat.id, "Your details are saved. I will now explain how to use the bot.", lang)
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
    text = f"You selected: {query.data}. Please describe this activity in text or voice message."
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
    # RESTORED: Conditionally show a button to return to the logging flow.
    
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
    # RESTORED: Handler logic for the 'jump_back_to_log' button.
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
        error_message = result.get("error", "An unknown error occurred.")
        await update.message.reply_text(f"Sorry, I couldn't complete the analysis. Reason: {error_message}")
        await text_to_speech_and_send(context, update.effective_chat.id, f"Sorry, I could not complete the analysis. Reason: {error_message}", lang)
    return await show_main_menu(update, context)

async def ask_start(update: Update, context: CallbackContext) -> int:
    lang = await get_user_lang(context, update.effective_user.id)
    text = "What is your agricultural question? You can send it as text or a voice message."
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
        await update.message.reply_text("Transcribing your voice message...")
        lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        voice_data = await voice_file.download_as_bytearray()
        audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
        processed_voice_data = audio.export(format="ogg", codec="libopus").read()
        query_text = await speech_to_text(processed_voice_data, lang_code=lang_code)
    if not query_text:
        await update.message.reply_text("I'm sorry, I couldn't understand your query. Please try again.")
        return AWAITING_QUERY
    await update.message.reply_text("Thinking... I'm looking up an answer for you. 🤔")
    result = await ask_query_on_backend(user_id, query_text, lang)
    if result and result.get("success"):
        response_text = result.get('response', 'I could not find an answer.')
        await update.message.reply_text(response_text)
        await text_to_speech_and_send(context, update.effective_chat.id, response_text, lang)
    else:
        error_message = result.get("error", "An unknown error occurred.")
        await update.message.reply_text(f"Sorry, I couldn't process your query. Reason: {error_message}")
        await text_to_speech_and_send(context, update.effective_chat.id, f"Sorry, I could not process your query. Reason: {error_message}", lang)
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



# import logging
# import os
# import io
# import datetime
# import pytz
# import aiohttp

# from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
# from telegram.ext import (
#     Application,
#     CommandHandler,
#     MessageHandler,
#     filters,
#     ConversationHandler,
#     CallbackContext,
#     CallbackQueryHandler
# )
# from google.cloud import texttospeech, speech
# from firebase_admin import credentials, firestore as admin_firestore, initialize_app
# from pydub import AudioSegment
# from geopy.geocoders import Nominatim
# from typing import Tuple

# # --- Configuration ---
# AudioSegment.converter = r"C:\ffmpeg\bin\ffmpeg.exe"
# TELEGRAM_TOKEN = "8415632607:AAE2H9jIDYX1zQkm7whRjVsBkwN1dpf4Gj8"
# FIRESTORE_PROJECT_ID = "handy-outpost-471016-c5"

# FASTAPI_BASE_URL = "http://127.0.0.1:8000"

# os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-credentials.json"

# # --- Initialization ---
# try:
#     cred = credentials.ApplicationDefault()
#     initialize_app(cred, {'projectId': FIRESTORE_PROJECT_ID})
#     db = admin_firestore.client()
# except Exception as e:
#     print(f"Could not initialize Firebase Admin SDK: {e}")
#     db = None

# speech_client = speech.SpeechClient()
# tts_client = texttospeech.TextToSpeechClient()
# geolocator = Nominatim(user_agent="telegram-questionnaire-bot")

# logging.basicConfig(
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
#     level=logging.INFO
# )
# logger = logging.getLogger(__name__)

# # --- State Definitions for ConversationHandler ---
# (
#     SELECTING_LANG, ASKING_QUESTION,
#     ASKING_LOCATION, CONFIRMING_LOCATION,
#     SELECTING_ACTIVITY, LOGGING_NOTE, CONFIRM_LOG_AGAIN,
#     MAIN_MENU, AWAITING_PEST_PHOTO, AWAITING_QUERY
# ) = range(10)

# # --- Bot Data ---
# QUESTIONS = {
#     'en': ["What is your name?", "What is your age?", "What is the area of your land in acres?", "What are the main crops you grow?"],
#     'ml': ["നിങ്ങളുടെ പേരെന്താണ്?", "നിങ്ങൾക്ക് എത്ര വയസ്സായി?", "നിങ്ങളുടെ ഭൂമിയുടെ വിസ്തീർണ്ണം ഏക്കറിൽ എത്രയാണ്?", "നിങ്ങൾ കൃഷി ചെയ്യുന്ന പ്രധാന വിളകൾ ഏതൊക്കെയാണ്?"]
# }

# BUTTON_LABELS = {
#     'en': {
#         'irrigation': "Irrigation 💧", 'fertilizing': "Fertilizing 🌱",
#         'pest_control': "Pest Control 🐞", 'weeding': "Weeding 🌿",
#         'sowing': "Sowing / Planting 🌾", 'harvesting': "Harvesting 🧺",
#         'other': "Other 🗒️", 'ask_queries': "Ask queries",
#         'analyze_leaf': "Analyze Leaf Image 📸", 'ask_general': "Ask a General Question ❓",
#         'return_logging': "Return to Logging 📝", 'finish_session': "Finish Session 👋",
#         'yes': "Yes", 'no': "No"
#     },
#     'ml': {
#         'irrigation': "ജലസേചനം 💧", 'fertilizing': "വളപ്രയോഗം 🌱",
#         'pest_control': "കീടനിയന്ത്രണം 🐞", 'weeding': "കളനിയന്ത്രണം 🌿",
#         'sowing': "വിതയ്ക്കൽ / നടീൽ 🌾", 'harvesting': "വിളവെടുപ്പ് 🧺",
#         'other': "മറ്റുള്ളവ 🗒️", 'ask_queries': "ചോദ്യങ്ങൾ ചോദിക്കുക",
#         'analyze_leaf': "ഇലയുടെ ചിത്രം വിശകലനം ചെയ്യുക 📸",
#         'ask_general': "ഒരു പൊതു ചോദ്യം ചോദിക്കുക ❓",
#         'return_logging': "ലോഗിംഗിലേക്ക് മടങ്ങുക 📝",
#         'finish_session': "സെഷൻ അവസാനിപ്പിക്കുക 👋",
#         'yes': "അതെ", 'no': "ഇല്ല"
#     }
# }

# VOICE_CONFIGS = {
#     'en': texttospeech.VoiceSelectionParams(language_code="en-US", ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL),
#     'ml': texttospeech.VoiceSelectionParams(language_code="ml-IN", name="ml-IN-Wavenet-C")
# }

# # --- Helper Functions ---
# async def text_to_speech_and_send(context: CallbackContext, chat_id: int, text: str, lang: str):
#     audio_content = await text_to_speech(text, lang)
#     if audio_content:
#         await context.bot.send_voice(chat_id=chat_id, voice=audio_content)

# async def text_to_speech(text: str, lang_code: str) -> bytes:
#     try:
#         synthesis_input = texttospeech.SynthesisInput(text=text)
#         audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.OGG_OPUS)
#         voice_params = VOICE_CONFIGS.get(lang_code, VOICE_CONFIGS['en'])
#         response = tts_client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_config)
#         return response.audio_content
#     except Exception as e:
#         logger.error(f"Text-to-speech error: {e}")
#         return b""

# async def speech_to_text(audio_data: bytes, lang_code: str) -> str:
#     audio = speech.RecognitionAudio(content=audio_data)
#     config = speech.RecognitionConfig(
#         encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
#         sample_rate_hertz=48000,
#         language_code=lang_code
#     )
#     try:
#         response = speech_client.recognize(config=config, audio=audio)
#         return response.results[0].alternatives[0].transcript if response.results else ""
#     except Exception as e:
#         logger.error(f"Speech-to-text error: {e}")
#     return ""

# async def get_address_from_coords(latitude: float, longitude: float) -> dict:
#     try:
#         location = geolocator.reverse((latitude, longitude), exactly_one=True, language='en')
#         if location and location.raw.get('address'):
#             address = location.raw['address']
#             district = address.get('county', address.get('state_district', 'Unknown'))
#             return {"full_address": location.address, "district": district}
#         return {"full_address": "Address not found.", "district": "Unknown"}
#     except Exception as e:
#         logger.error(f"Geocoding error: {e}")
#         return {"full_address": "Could not fetch address.", "district": "Error"}

# async def get_user_lang(context: CallbackContext, user_id: int) -> str:
#     if 'lang' in context.user_data:
#         return context.user_data['lang']
#     if db:
#         user_doc = db.collection('questionnaire_responses').document(str(user_id)).get()
#         if user_doc.exists:
#             lang = user_doc.to_dict().get('language', 'en')
#             context.user_data['lang'] = lang
#             return lang
#     return 'en'

# async def register_user_on_backend(user_id: int, user_data: dict):
#     url = f"{FASTAPI_BASE_URL}/register"
#     try:
#         answers = user_data.get('answers', {})
#         age = int(answers.get(QUESTIONS['en'][1], 0)) if answers.get(QUESTIONS['en'][1]) else 0
#         payload = {
#             "user_name": str(user_id), "age": age,
#             "district": user_data.get("district", "Unknown"),
#             "crops_grown": answers.get(QUESTIONS['en'][3], "Unknown"),
#             "farm_size": answers.get(QUESTIONS['en'][2], "0")
#         }
#         async with aiohttp.ClientSession() as session:
#             async with session.post(url, json=payload) as response:
#                 if response.status == 200:
#                     logger.info(f"Successfully registered user {user_id} on the backend.")
#                     return True
#                 else:
#                     logger.error(f"Failed to register user {user_id}. Status: {response.status}, Response: {await response.text()}")
#                     return False
#     except Exception as e:
#         logger.error(f"Exception during backend registration: {e}")
#         return False

# async def analyze_pest_image_on_backend(user_id: int, photo_data: bytes, lang_code: str) -> dict:
#     url = f"{FASTAPI_BASE_URL}/analyze"
#     data = aiohttp.FormData()
#     data.add_field('user_name', str(user_id))
#     data.add_field('lang_code', lang_code)
#     data.add_field('file', photo_data, filename='photo.jpg', content_type='image/jpeg')
#     try:
#         async with aiohttp.ClientSession() as session:
#             async with session.post(url, data=data) as response:
#                 return await response.json() if response.status == 200 else {"success": False, "error": f"API Error: {response.status}"}
#     except Exception as e:
#         return {"success": False, "error": f"Connection Error: {e}"}

# async def ask_query_on_backend(user_id: int, query: str, lang_code: str) -> dict:
#     url = f"{FASTAPI_BASE_URL}/query_text"
#     payload = {"user_name": str(user_id), "query_text": query, "lang_code": lang_code}
#     try:
#         async with aiohttp.ClientSession() as session:
#             async with session.post(url, json=payload) as response:
#                 return await response.json() if response.status == 200 else {"success": False, "error": f"API Error: {response.status}"}
#     except Exception as e:
#         return {"success": False, "error": f"Connection Error: {e}"}

# # --- Questionnaire Flow ---
# async def start(update: Update, context: CallbackContext) -> int:
#     context.user_data.clear()
#     text = "Welcome! Please choose your language. Say 'English' or 'മലയാളം'."
#     await update.message.reply_text(text)
#     await text_to_speech_and_send(context, update.effective_chat.id, text, 'en')
#     return SELECTING_LANG

# async def select_language(update: Update, context: CallbackContext) -> int:
#     user_input = ""
#     if update.message.text:
#         user_input = update.message.text.lower()
#     elif update.message.voice:
#         voice_file = await context.bot.get_file(update.message.voice.file_id)
#         voice_data = await voice_file.download_as_bytearray()
#         audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
#         processed_voice_data = audio.export(format="ogg", codec="libopus").read()
#         transcript_en = await speech_to_text(processed_voice_data, 'en-US')
#         transcript_ml = await speech_to_text(processed_voice_data, 'ml-IN')
#         user_input = (transcript_en + " " + transcript_ml).lower()

#     if "english" in user_input:
#         context.user_data['lang'] = 'en'
#         await update.message.reply_text("You have selected English.")
#     elif "മലയാളം" in user_input or "malayalam" in user_input:
#         context.user_data['lang'] = 'ml'
#         await update.message.reply_text("നിങ്ങൾ മലയാളം തിരഞ്ഞെടുത്തു.")
#     else:
#         await update.message.reply_text("Sorry, I didn't understand. Please say 'English' or 'മലയാളം'.")
#         return SELECTING_LANG

#     context.user_data['question_index'] = 0
#     context.user_data['answers'] = {}
#     await ask_next_question(update, context)
#     return ASKING_QUESTION

# async def ask_next_question(update: Update, context: CallbackContext):
#     message = update.effective_message
#     chat_id = update.effective_chat.id
#     lang = context.user_data['lang']
#     q_index = context.user_data.get('question_index', 0)
#     question_text = QUESTIONS[lang][q_index]
#     await message.reply_text(question_text)
#     await text_to_speech_and_send(context, chat_id, question_text, lang)

# async def handle_answer(update: Update, context: CallbackContext) -> int:
#     lang = context.user_data['lang']
#     q_index = context.user_data['question_index']
#     question_text_en = QUESTIONS['en'][q_index]
#     answer_text = ""
    
#     if update.message.text:
#         answer_text = update.message.text
#     elif update.message.voice:
#         lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
#         voice_file = await context.bot.get_file(update.message.voice.file_id)
#         voice_data = await voice_file.download_as_bytearray()
#         audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
#         processed_voice_data = audio.export(format="ogg", codec="libopus").read()
#         answer_text = await speech_to_text(processed_voice_data, lang_code=lang_code)

#     if answer_text:
#         context.user_data['answers'][question_text_en] = answer_text
#         if q_index == 1:
#             return await ask_for_location(update, context)
#         context.user_data['question_index'] += 1
#         if context.user_data['question_index'] < len(QUESTIONS[lang]):
#             await ask_next_question(update, context)
#             return ASKING_QUESTION
#         else:
#             return await save_questionnaire_and_start_logging(update, context)
#     else:
#         fail_text = "Sorry, I couldn't understand. Please try again."
#         await update.message.reply_text(fail_text)
#         await text_to_speech_and_send(context, update.effective_chat.id, fail_text, lang)
#         return ASKING_QUESTION

# async def ask_for_location(update: Update, context: CallbackContext) -> int:
#     message = update.effective_message
#     chat_id = update.effective_chat.id
#     if update.callback_query:
#         await update.callback_query.edit_message_text(text="Location incorrect. Let's try again.")
#     lang = context.user_data.get('lang', 'en')
#     location_button = KeyboardButton(text="📍 Share My Location", request_location=True)
#     reply_markup = ReplyKeyboardMarkup([[location_button]], resize_keyboard=True, one_time_keyboard=True)
#     text = "Thank you. To find your location, please tap the button below."
#     await message.reply_text(text, reply_markup=reply_markup)
#     await text_to_speech_and_send(context, chat_id, text, lang)
#     return ASKING_LOCATION

# async def handle_location(update: Update, context: CallbackContext) -> int:
#     if not update.message.location:
#         await update.message.reply_text("Please use the button to send your location.")
#         return ASKING_LOCATION
#     lang = context.user_data.get('lang', 'en')
#     await update.message.reply_text("Got it! Looking up your address...")
#     address_info = await get_address_from_coords(update.message.location.latitude, update.message.location.longitude)
#     context.user_data['full_address'] = address_info['full_address']
#     context.user_data['district'] = address_info['district']
#     text_for_display = f"I see you are near:\n📍 {address_info['full_address']}\n\nIs this correct?"
#     text_for_speech = f"You are near {address_info['full_address']}. Is this correct?"
#     labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
#     keyboard = [[
#         InlineKeyboardButton(labels['yes'], callback_data='loc_confirm_yes'),
#         InlineKeyboardButton(labels['no'], callback_data='loc_confirm_no')
#     ]]
#     reply_markup = InlineKeyboardMarkup(keyboard)
#     await update.message.reply_text(text_for_display, reply_markup=reply_markup)
#     await text_to_speech_and_send(context, update.effective_chat.id, text_for_speech, lang)
#     return CONFIRMING_LOCATION

# async def handle_location_confirmation(update: Update, context: CallbackContext) -> int:
#     query = update.callback_query
#     await query.answer()
#     lang = context.user_data.get('lang', 'en')
#     if query.data == 'loc_confirm_yes':
#         context.user_data['answers']['Fetched Address'] = context.user_data.get('full_address', 'N/A')
#         await query.edit_message_text(text=f"Location confirmed: {context.user_data['full_address']}")
#         context.user_data['question_index'] += 1
#         if context.user_data['question_index'] < len(QUESTIONS[lang]):
#             await ask_next_question(update, context)
#             return ASKING_QUESTION
#         else:
#             return await save_questionnaire_and_start_logging(update, context)
#     elif query.data == 'loc_confirm_no':
#         return await ask_for_location(update, context)
#     return CONFIRMING_LOCATION

# async def save_questionnaire_and_start_logging(update: Update, context: CallbackContext) -> int:
#     message = update.effective_message
#     user = update.effective_user
#     lang = context.user_data.get('lang', 'en')
#     await message.reply_text("Thank you! Saving your details...")
#     if db:
#         user_doc_ref = db.collection('questionnaire_responses').document(str(user.id))
#         data_to_save = {
#             'user_id': user.id, 'username': user.username, 'language': lang,
#             'responses': context.user_data.get('answers', {}),
#             'timestamp': admin_firestore.SERVER_TIMESTAMP
#         }
#         user_doc_ref.set(data_to_save)
#     await register_user_on_backend(user.id, context.user_data)
#     text = (
#         "Your details are saved! Here's how you can use this bot:\n\n"
#         "**Guided Experience (Recommended):**\n"
#         "We will now start your first activity log. After logging, a menu will appear to help you use the AI Assistant.\n\n"
#         "**Manual Commands (Anytime):**\n"
#         "You can also click the following commands at any time:\n"
#         "• /log - Start logging a new farm activity.\n"
#         "• /pest - Analyze a plant leaf photo for disease.\n"
#         "• /ask - Ask any agricultural question.\n\n"
#         "Let's begin with your first activity log."
#     )
#     await message.reply_text(text, parse_mode='Markdown')
#     await text_to_speech_and_send(context, update.effective_chat.id, "Your details are saved. I will now explain how to use the bot.", lang)
#     return await prompt_for_activity(update, context)

# # --- Activity Logging Flow ---
# async def log_start(update: Update, context: CallbackContext) -> int:
#     user_id = update.effective_user.id
#     context.user_data['lang'] = await get_user_lang(context, user_id)
#     await update.message.reply_text("Starting a new logging session...")
#     return await prompt_for_activity(update, context)

# async def prompt_for_activity(update: Update, context: CallbackContext) -> int:
#     lang = context.user_data.get('lang', 'en')
#     labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
#     keyboard = [
#         [InlineKeyboardButton(labels['irrigation'], callback_data='Irrigation'), InlineKeyboardButton(labels['fertilizing'], callback_data='Fertilizing')],
#         [InlineKeyboardButton(labels['pest_control'], callback_data='Pest Control'), InlineKeyboardButton(labels['weeding'], callback_data='Weeding')],
#         [InlineKeyboardButton(labels['sowing'], callback_data='Sowing'), InlineKeyboardButton(labels['harvesting'], callback_data='Harvesting')],
#         [InlineKeyboardButton(labels['ask_queries'], callback_data='jump_to_ai'), InlineKeyboardButton(labels['other'], callback_data='Other')]
#     ]
#     reply_markup = InlineKeyboardMarkup(keyboard)
#     text = "📝 Please choose the activity you want to log, or ask the AI assistant."
#     if update.callback_query:
#         await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
#     else:
#         await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup)
#     await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
#     return SELECTING_ACTIVITY

# async def log_activity_button(update: Update, context: CallbackContext) -> int:
#     query = update.callback_query
#     await query.answer()
#     if query.data == 'jump_to_ai':
#         context.user_data['came_from_logging'] = True
#         return await show_main_menu(update, context)
#     context.user_data['current_activity'] = query.data
#     lang = context.user_data.get('lang', 'en')
#     text = f"You selected: {query.data}. Please describe this activity via text or voice message."
#     await query.edit_message_text(text=text)
#     await text_to_speech_and_send(context, query.message.chat_id, text, lang)
#     return LOGGING_NOTE

# async def log_note(update: Update, context: CallbackContext) -> int:
#     note_text = ""
#     lang = context.user_data.get('lang', 'en')
#     if update.message.text:
#         note_text = update.message.text
#     elif update.message.voice:
#         lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
#         voice_file = await context.bot.get_file(update.message.voice.file_id)
#         voice_data = await voice_file.download_as_bytearray()
#         audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
#         processed_voice_data = audio.export(format="ogg", codec="libopus").read()
#         note_text = await speech_to_text(processed_voice_data, lang_code=lang_code)
#     if not note_text:
#         await update.message.reply_text("Sorry, I couldn't understand. Please try sending the note again.")
#         return LOGGING_NOTE
#     try:
#         user = update.message.from_user
#         log_data = {
#             'activity_type': context.user_data.get('current_activity'),
#             'note': note_text, 'timestamp': admin_firestore.SERVER_TIMESTAMP
#         }
#         if db:
#             db.collection('questionnaire_responses').document(str(user.id)).collection('activity_logs').add(log_data)
#         labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
#         keyboard = [[
#             InlineKeyboardButton(labels['yes'], callback_data='log_again_yes'),
#             InlineKeyboardButton(labels['no'], callback_data='log_again_no')
#         ]]
#         reply_markup = InlineKeyboardMarkup(keyboard)
#         confirm_text = "✅ Your activity has been logged! Would you like to log another one?"
#         await update.message.reply_text(confirm_text, reply_markup=reply_markup)
#         await text_to_speech_and_send(context, update.effective_chat.id, confirm_text, lang)
#         return CONFIRM_LOG_AGAIN
#     except Exception as e:
#         logger.error(f"Error saving log: {e}")
#         await update.message.reply_text("Sorry, there was an error saving your log.")
#         return ConversationHandler.END

# async def log_again_or_end(update: Update, context: CallbackContext) -> int:
#     query = update.callback_query
#     await query.answer()
#     if query.data == 'log_again_yes':
#         return await prompt_for_activity(update, context)
#     else:
#         # MODIFIED: Set a flag for the one-time message
#         context.user_data['just_finished_logging'] = True
#         return await show_main_menu(update, context)

# # --- AI Assistant Menu and Flows ---
# async def show_main_menu(update: Update, context: CallbackContext) -> int:
#     lang = await get_user_lang(context, update.effective_user.id)
#     labels = BUTTON_LABELS.get(lang, BUTTON_LABELS['en'])
#     keyboard = [
#         [InlineKeyboardButton(labels['analyze_leaf'], callback_data='ask_pest')],
#         [InlineKeyboardButton(labels['ask_general'], callback_data='ask_general')]
#     ]
#     if context.user_data.get('came_from_logging'):
#         keyboard.append([InlineKeyboardButton(labels['return_logging'], callback_data='jump_back_to_log')])
#     keyboard.append([InlineKeyboardButton(labels['finish_session'], callback_data='end_session')])
#     reply_markup = InlineKeyboardMarkup(keyboard)
    
#     # MODIFIED: Use a one-time flag for the message after logging
#     if context.user_data.pop('just_finished_logging', False):
#          text = "✅ Your activities are logged. What would you like to do next?"
#     else:
#         text = "🤖 AI Assistant Menu. What would you like to do next?"

#     if update.callback_query:
#         await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
#     else:
#         await context.bot.send_message(update.effective_chat.id, text, reply_markup=reply_markup)
#     await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
#     return MAIN_MENU

# async def main_menu_handler(update: Update, context: CallbackContext) -> int:
#     query = update.callback_query
#     await query.answer()
#     if query.data == 'jump_back_to_log':
#         context.user_data.pop('came_from_logging', None)
#         return await prompt_for_activity(update, context)
#     elif query.data == 'ask_pest':
#         return await pest_start(update, context)
#     elif query.data == 'ask_general':
#         return await ask_start(update, context)
#     elif query.data == 'end_session':
#         return await end_session(update, context)
#     return MAIN_MENU

# async def pest_start(update: Update, context: CallbackContext) -> int:
#     lang = await get_user_lang(context, update.effective_user.id)
#     text = "🌿 To analyze for pests or diseases, please send me a clear photo of the affected plant leaf."
#     if update.callback_query:
#         await update.callback_query.edit_message_text(text)
#     else:
#         await update.message.reply_text(text)
#     await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
#     return AWAITING_PEST_PHOTO

# async def handle_pest_photo(update: Update, context: CallbackContext) -> int:
#     lang = await get_user_lang(context, update.effective_user.id)
#     await update.message.reply_text("Analyzing the image... This may take a moment. 🔬")
#     photo_file = await update.message.photo[-1].get_file()
#     photo_data = await photo_file.download_as_bytearray()
#     result = await analyze_pest_image_on_backend(update.effective_user.id, photo_data, lang)
#     if result and result.get("success"):
#         description = result.get('description', 'No description provided.')
#         diagnosis = result.get('diagnosis', 'No diagnosis provided.')
#         response_text = f"--- **Image Description** ---\n{description}\n\n--- **Diagnosis & Recommendations** ---\n{diagnosis}"
#         await update.message.reply_text(response_text)
#         await text_to_speech_and_send(context, update.effective_chat.id, diagnosis, lang)
#     else:
#         error_message = result.get("error", "An unknown error occurred.")
#         await update.message.reply_text(f"Sorry, I couldn't complete the analysis. Reason: {error_message}")
#         await text_to_speech_and_send(context, update.effective_chat.id, f"Sorry, I could not complete the analysis. Reason: {error_message}", lang)
#     return await show_main_menu(update, context)

# async def ask_start(update: Update, context: CallbackContext) -> int:
#     lang = await get_user_lang(context, update.effective_user.id)
#     text = "💬 What is your agricultural question? You can send it as text or a voice message."
#     if update.callback_query:
#         await update.callback_query.edit_message_text(text)
#     else:
#         await update.message.reply_text(text)
#     await text_to_speech_and_send(context, update.effective_chat.id, text, lang)
#     return AWAITING_QUERY

# async def handle_query(update: Update, context: CallbackContext) -> int:
#     user_id = update.effective_user.id
#     lang = await get_user_lang(context, user_id)
#     query_text = ""
#     if update.message.text:
#         query_text = update.message.text
#     elif update.message.voice:
#         await update.message.reply_text("Transcribing your voice message...")
#         lang_code = f"{lang}-IN" if lang == 'ml' else f"{lang}-US"
#         voice_file = await context.bot.get_file(update.message.voice.file_id)
#         voice_data = await voice_file.download_as_bytearray()
#         audio = AudioSegment.from_ogg(io.BytesIO(voice_data))
#         processed_voice_data = audio.export(format="ogg", codec="libopus").read()
#         query_text = await speech_to_text(processed_voice_data, lang_code=lang_code)
#     if not query_text:
#         await update.message.reply_text("I'm sorry, I couldn't understand your query. Please try again.")
#         return AWAITING_QUERY
#     await update.message.reply_text("Thinking... I'm looking up an answer for you. 🤔")
#     result = await ask_query_on_backend(user_id, query_text, lang)
#     if result and result.get("success"):
#         response_text = result.get('response', 'I could not find an answer.')
#         await update.message.reply_text(response_text)
#         await text_to_speech_and_send(context, update.effective_chat.id, response_text, lang)
#     else:
#         error_message = result.get("error", "An unknown error occurred.")
#         await update.message.reply_text(f"Sorry, I couldn't process your query. Reason: {error_message}")
#         await text_to_speech_and_send(context, update.effective_chat.id, f"Sorry, I could not process your query. Reason: {error_message}", lang)
#     return await show_main_menu(update, context)

# # --- General Handlers ---
# async def end_session(update: Update, context: CallbackContext) -> int:
#     text = "You're all set. Have a great day! 👋"
#     if update.callback_query:
#         await update.callback_query.edit_message_text(text=text)
#     else:
#         await update.message.reply_text(text)
#     context.user_data.clear()
#     return ConversationHandler.END

# async def cancel(update: Update, context: CallbackContext) -> int:
#     await update.message.reply_text('Operation cancelled.')
#     context.user_data.clear()
#     return ConversationHandler.END

# def main():
#     application = Application.builder().token(TELEGRAM_TOKEN).build()
#     conv_handler = ConversationHandler(
#         entry_points=[
#             CommandHandler('start', start),
#             CommandHandler('log', log_start),
#             CommandHandler('pest', pest_start),
#             CommandHandler('ask', ask_start),
#         ],
#         states={
#             SELECTING_LANG: [MessageHandler(filters.TEXT | filters.VOICE, select_language)],
#             ASKING_QUESTION: [MessageHandler(filters.TEXT | filters.VOICE, handle_answer)],
#             ASKING_LOCATION: [MessageHandler(filters.LOCATION, handle_location)],
#             CONFIRMING_LOCATION: [CallbackQueryHandler(handle_location_confirmation)],
#             SELECTING_ACTIVITY: [CallbackQueryHandler(log_activity_button)],
#             LOGGING_NOTE: [MessageHandler(filters.TEXT | filters.VOICE & ~filters.COMMAND, log_note)],
#             CONFIRM_LOG_AGAIN: [CallbackQueryHandler(log_again_or_end)],
#             MAIN_MENU: [CallbackQueryHandler(main_menu_handler)],
#             AWAITING_PEST_PHOTO: [MessageHandler(filters.PHOTO, handle_pest_photo)],
#             AWAITING_QUERY: [MessageHandler(filters.TEXT | filters.VOICE, handle_query)],
#         },
#         fallbacks=[CommandHandler('cancel', cancel), CommandHandler('end', end_session)],
#         allow_reentry=True
#     )
#     application.add_handler(conv_handler)
#     application.run_polling()

# if __name__ == '__main__':
#     main()

