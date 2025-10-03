from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import openai
import google.generativeai as genai
import base64
from typing import TypedDict, Dict, Any, Optional
from langgraph.graph import StateGraph
import asyncio
import io
import json
from pydantic import BaseModel
import hashlib
from datetime import datetime
import traceback
from uuid import uuid4
from neo4j import GraphDatabase

from langchain_neo4j import Neo4jChatMessageHistory, Neo4jGraph
from langchain_core.messages import HumanMessage, AIMessage

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY must be set in your .env file.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY must be set in your .env file.")

openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
genai.configure(api_key=GEMINI_API_KEY)


neo4j_graph = Neo4jGraph(
    url="bolt://localhost:7687",
    username="neo4j",
    password="Telebot1234"
)

neo4j_driver = GraphDatabase.driver(
    "bolt://localhost:7687",
    auth=("neo4j", "Telebot1234")
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class UserRegistration(BaseModel):
    user_name: str
    age: int = None
    district: str = None
    crops_grown: str = None
    farm_size: str = None
    contact: str = None


registered_users = {}
user_sessions = {}

class TextQueryRequest(BaseModel):
    user_name: str
    query_text: str
    lang_code: str

@app.post("/text_query")
async def handle_text_query(request: TextQueryRequest):
    user_name = request.user_name
    query_text = request.query_text.strip()
    lang_code = request.lang_code

    if user_name not in registered_users:
        raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

    if not query_text:
        raise HTTPException(status_code=400, detail="Query text cannot be empty.")

    try:
        initial_state: QueryState = {
            "query_text": query_text,
            "llm_response": "",
            "user_id": user_name,
            "lang_code": lang_code
        }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

        llm_response = result.get("llm_response", "Sorry, I could not process that.")

        add_to_conversation(
            user_name,
            human_message=query_text,
            ai_message=llm_response
        )

        return JSONResponse({
            "success": True,
            "transcribed_text": query_text,
            "response": llm_response
        })
    except Exception as e:
        print(f"Error in handle_text_query: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

def get_or_create_session_id(user_name: str) -> str:
    """Get existing session ID or create new one for user"""
    if user_name not in user_sessions:
        user_sessions[user_name] = str(uuid4())
        print(f"Created session ID {user_sessions[user_name]} for user {user_name}")
    return user_sessions[user_name]

def get_chat_memory(user_name: str) -> Neo4jChatMessageHistory:
    """Get Neo4j chat memory for specific user"""
    session_id = get_or_create_session_id(user_name)
    return Neo4jChatMessageHistory(
        session_id=session_id,
        graph=neo4j_graph
    )

def store_user_profile(user_id: str, profile_data: Dict[str, Any]):
    """Store user profile in Neo4j"""
    try:
        with neo4j_driver.session() as session:
            session.run("""
                MERGE (u:UserProfile {user_id: $user_id})
                SET u.user_name = $user_name,
                    u.age = $age,
                    u.district = $district,
                    u.crops_grown = $crops_grown,
                    u.farm_size = $farm_size,
                    u.contact = $contact,
                    u.registration_date = $registration_date,
                    u.session_id = $session_id,
                    u.last_updated = $last_updated
                RETURN u
            """,
                user_id=user_id,
                user_name=profile_data.get("user_name"),
                age=profile_data.get("age"),
                district=profile_data.get("district"),
                crops_grown=profile_data.get("crops_grown"),
                farm_size=profile_data.get("farm_size"),
                contact=profile_data.get("contact"),
                registration_date=profile_data.get("registration_date"),
                session_id=get_or_create_session_id(user_id),
                last_updated=datetime.now().isoformat()
            )
        return True
    except Exception as e:
        print(f"Error storing user profile: {e}")
        return False

def get_user_profile(user_id: str) -> Dict[str, Any]:
    """Get user profile from Neo4j"""
    try:
        with neo4j_driver.session() as session:
            result = session.run("""
                MATCH (u:UserProfile {user_id: $user_id})
                RETURN u.user_name as user_name,
                       u.age as age,
                       u.district as district,
                       u.crops_grown as crops_grown,
                       u.farm_size as farm_size,
                       u.contact as contact,
                       u.registration_date as registration_date,
                       u.session_id as session_id,
                       u.last_updated as last_updated
            """, user_id=user_id)

            record = result.single()
            if record:
                if record["session_id"]:
                    user_sessions[user_id] = record["session_id"]

                return {
                    "user_id": user_id,
                    "user_name": record["user_name"],
                    "age": record["age"],
                    "district": record["district"],
                    "crops_grown": record["crops_grown"],
                    "farm_size": record["farm_size"],
                    "contact": record["contact"],
                    "registration_date": record["registration_date"],
                    "session_id": record["session_id"],
                    "last_updated": record["last_updated"]
                }
            return None
    except Exception as e:
        print(f"Error getting user profile: {e}")
        return None

def get_conversation_history(user_name: str) -> list:
    """Get conversation history using LangChain Neo4j memory"""
    try:
        chat_memory = get_chat_memory(user_name)
        messages = chat_memory.messages

        history = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                history.append({
                    "type": "human",
                    "content": msg.content,
                    "timestamp": datetime.now().isoformat()
                })
            elif isinstance(msg, AIMessage):
                history.append({
                    "type": "ai",
                    "content": msg.content,
                    "timestamp": datetime.now().isoformat()
                })

        return history
    except Exception as e:
        print(f"Error getting conversation history: {e}")
        return []

def add_to_conversation(user_name: str, human_message: str = None, ai_message: str = None):
    """Add message to conversation history"""
    try:
        chat_memory = get_chat_memory(user_name)

        if human_message:
            chat_memory.add_user_message(human_message)
            print(f" Added human message for {user_name}")

        if ai_message:
            chat_memory.add_ai_message(ai_message)
            print(f"Added AI message for {user_name}")

        return True
    except Exception as e:
        print(f"Error adding to conversation: {e}")
        return False

def get_relevant_context(user_name: str, query: str) -> str:
    """Get relevant context from user's conversation history"""
    try:
        history = get_conversation_history(user_name)

        profile = get_user_profile(user_name)
        context_parts = []

        if profile:
            profile_context = f"User: {profile.get('user_name')} from {profile.get('district', 'Unknown')} grows {profile.get('crops_grown', 'various crops')}"
            context_parts.append(profile_context)

        for msg in history[-6:]:
            if msg["type"] == "human":
                context_parts.append(f"User asked: {msg['content'][:150]}...")
            elif msg["type"] == "ai":
                context_parts.append(f"AI responded: {msg['content'][:150]}...")

        return "\n".join(context_parts) if context_parts else ""

    except Exception as e:
        print(f"Error getting relevant context: {e}")
        return ""

class PestState(TypedDict):
    image_b64: str
    description: str
    diagnosis: str
    user_id: str
    lang_code: str

class QueryState(TypedDict):
    query_text: str
    llm_response: str
    user_id: str
    lang_code: str

def describe_leaf(state: PestState) -> Dict[str, Any]:
    if "image_b64" not in state or not state["image_b64"]:
        return {"error": "No image found in input."}

    user_id = state.get("user_id", "farmer_system")
    lang_code = state.get("lang_code", "en")

    language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

    # MODIFIED: New, more concise prompt for description
    prompt = (
        f"""As an expert plant pathologist, analyze this leaf image.
        
        Instructions:
        1. Describe the most prominent visual symptoms (like spots, discoloration, or damage) in 2-3 concise sentences.
        2. Focus only on what is visible. Do not diagnose yet.
        3. Do not use any markdown like asterisks.

        Keep the description very short and direct.
        {language_instruction}"""
    )

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{state['image_b64']}"}}
            ]
        }
    ]

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            max_tokens=800
        )
        desc = response.choices[0].message.content or ""
        return {"description": desc, "user_id": user_id, "lang_code": lang_code}
    except Exception as e:
        print(f"ERROR in describe_leaf: {e}")
        return {"error": f"Vision model error: {str(e)}", "user_id": user_id}

def diagnose_leaf(state: PestState) -> Dict[str, Any]:
    description = state.get("description", "")
    user_id = state.get("user_id", "farmer_system")
    lang_code = state.get("lang_code", "en")

    if not description:
        return {"error": "No description provided for diagnosis.", "user_id": user_id}

    context = get_relevant_context(user_id, description)
    language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

    memory_context = ""
    if context:
        memory_context = f"""
        Here is some context about the user and recent conversation:
        {context}
        Use this to tailor your advice.
        """
    # MODIFIED: New, more concise prompt for diagnosis
    prompt = (
        f"""You are an expert plant pathologist for Kerala crops. Based on the leaf description below, provide a concise diagnosis.

        {memory_context}

        Leaf Description: {description}

        Instructions:
        1. Diagnosis: Name the most likely disease or pest.
        2. Summary: Briefly explain the issue in 1-2 sentences.
        3. Actions: List 2-3 clear, actionable steps for treatment. Use simple bullet points (e.g., "- Action 1").
        4. Follow-up: Ask if the user wants more details on treatment or prevention.

        The response must be short, direct, and easy for a farmer to understand. Do not use any markdown like asterisks.
        {language_instruction}
        """
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        diag = response.choices[0].message.content or ""

        return {"diagnosis": diag, "user_id": user_id}
    except Exception as e:
        print(f"ERROR in diagnose_leaf: {e}")
        return {"error": f"Diagnosis model error: {str(e)}", "user_id": user_id}

def process_query(state: QueryState) -> Dict[str, Any]:
    query_text = state.get("query_text", "")
    user_id = state.get("user_id", "farmer_system")
    lang_code = state.get("lang_code", "en")

    if not query_text:
        return {"error": "No query text provided.", "user_id": user_id}

    context = get_relevant_context(user_id, query_text)
    language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

    memory_context = ""
    if context:
        memory_context = f"""
        Here is some context about the user and recent conversation:
        {context}
        Use this to provide informed agricultural advice.
        """

    # MODIFIED: New, more concise prompt for general queries
    prompt = (
        f"""You are a top agricultural expert for Kerala. A farmer has a query.

        {memory_context}

        Farmer's Query: {query_text}

        Instructions:
        1. Answer: Give a short, direct answer to the main question.
        2. Actions: List the 2-3 most important actions as simple bullet points (e.g., "- Action 1").
        3. Follow-up: Ask if they need more details.

        Be concise and practical. Avoid long paragraphs and do not use any markdown like asterisks.
        {language_instruction}
        """
    )

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        answer = response.choices[0].message.content or ""

        return {"llm_response": answer, "user_id": user_id}
    except Exception as e:
        print(f"ERROR in process_query: {e}")
        return {"error": f"Query processing error: {str(e)}", "user_id": user_id}

async def transcribe_audio_with_gemini(audio_data: bytes, filename: str) -> str:
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')

        # NOTE: Gemini API expects a file-like object or raw bytes for audio.
        # The dictionary format used here is specific to some client libraries but might not be standard.
        # Let's assume this works as intended in the user's environment. For robustness, using a direct
        # content part would be `genai.types.Part(inline_data=audio_data, mime_type="audio/wav")`
        audio_file = {
            "mime_type": "audio/wav",
            "data": audio_data
        }

        prompt = (
            "Please transcribe the following audio file. The audio may contain speech in Malayalam, English, or other languages. "
            "Provide the transcription in the original language spoken. If Malayalam is detected, provide transcription in Malayalam script. "
            "Focus on agricultural, farming, or plant-related content."
        )

        response = model.generate_content([prompt, audio_file])

        if response.text:
            return response.text.strip()
        else:
            raise Exception("No transcription returned from Gemini")

    except Exception as e:
        print(f"Gemini transcription error: {e}")
        raise Exception(f"Gemini transcription failed: {str(e)}")

def create_pest_workflow():
    pest_graph = StateGraph(PestState)
    pest_graph.add_node("describe_leaf", describe_leaf)
    pest_graph.add_node("diagnose_leaf", diagnose_leaf)
    pest_graph.add_edge("describe_leaf", "diagnose_leaf")
    pest_graph.set_entry_point("describe_leaf")
    pest_graph.set_finish_point("diagnose_leaf")
    return pest_graph.compile()

def create_query_workflow():
    query_graph = StateGraph(QueryState)
    query_graph.add_node("process_query", process_query)
    query_graph.set_entry_point("process_query")
    query_graph.set_finish_point("process_query")
    return query_graph.compile()

pest_flow = create_pest_workflow()
query_flow = create_query_workflow()

@app.get("/")
def root():
    return {
        "message": "Plant Disease Diagnosis API with LangChain Neo4j Chat Memory",
        "endpoints": {
            "/register": "POST - Register user",
            "/analyze": "POST - Upload leaf image",
            "/query": "POST - Upload audio file",
            "/text_query": "POST - Submit a text query",
            "/user/{user_name}": "GET - Get user info",
            "/user/{user_name}/history": "GET - Get chat history"
        },
        "features": {
            "langchain_memory": "Uses Neo4jChatMessageHistory for conversation persistence",
            "session_management": "Each user gets unique session ID",
            "graph_storage": "Messages stored as connected nodes with NEXT relationships"
        },
        "status": "Running with LangChain Neo4j Chat Memory"
    }

@app.post("/register")
async def register_user(user_data: UserRegistration):
    try:
        user_name = user_data.user_name.strip()
        if not user_name:
            raise HTTPException(status_code=400, detail="User name cannot be empty")

        user_id = user_name

        user_info = {
            "user_id": user_id,
            "user_name": user_name,
            "age": user_data.age,
            "district": user_data.district,
            "crops_grown": user_data.crops_grown,
            "farm_size": user_data.farm_size,
            "contact": user_data.contact,
            "registration_date": datetime.now().isoformat()
        }

        registered_users[user_id] = user_info

        profile_stored = store_user_profile(user_id, user_info)

        session_id = get_or_create_session_id(user_name)
        registration_msg = f"User {user_name} registered from {user_data.district or 'Unknown district'}"
        chat_stored = add_to_conversation(user_name, human_message=registration_msg)

        return {
            "success": True,
            "message": f"User {user_name} registered with chat memory",
            "user_id": user_id,
            "user_name": user_name,
            "session_id": session_id,
            "profile_stored": profile_stored,
            "chat_history_started": chat_stored,
            "storage_info": f"Profile and chat history stored in Neo4j for: {user_name}"
        }

    except Exception as e:
        print(f"Registration error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

@app.post("/analyze")
async def analyze_leaf(user_name: str = Form(...), lang_code: str = Form(...), file: UploadFile = File(...)):
    if user_name not in registered_users:
        raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

    allowed_types = ["image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"Only JPEG and PNG images supported.")

    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        b64_image = base64.b64encode(contents).decode('utf-8')

        initial_state: PestState = {
            "image_b64": b64_image,
            "description": "",
            "diagnosis": "",
            "user_id": user_name,
            "lang_code": lang_code
        }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, pest_flow.invoke, initial_state)

        description = result.get("description", "")
        diagnosis = result.get("diagnosis", "")
        error = result.get("error", "")

        if error:
            raise HTTPException(status_code=500, detail=error)

        human_msg = f"Analyzed leaf image: {file.filename}"
        ai_msg = f"Description: {description[:100]}... Diagnosis: {diagnosis[:100]}..."

        chat_stored = add_to_conversation(
            user_name,
            human_message=human_msg,
            ai_message=ai_msg
        )

        return JSONResponse({
            "success": True,
            "message": f"Analysis completed and added to chat history for {user_name}",
            "user_name": user_name,
            "chat_history_updated": chat_stored,
            "description": description,
            "diagnosis": diagnosis,
            "storage_info": f"Added to Neo4j chat memory for: {user_name}"
        })

    except Exception as e:
        print(f"Error in analyze_leaf: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/query")
async def handle_query(user_name: str = Form(...), audio: UploadFile = File(...)):
    if user_name not in registered_users:
        raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

    try:
        audio_contents = await audio.read()
        if not audio_contents:
            raise HTTPException(status_code=400, detail="Uploaded audio file is empty.")

        query_text = await transcribe_audio_with_gemini(audio_contents, audio.filename or "audio.wav")

        if not query_text or query_text.strip() == "":
            raise HTTPException(status_code=400, detail="Could not transcribe audio.")

        lang_code = "ml"

        initial_state: QueryState = {
            "query_text": query_text.strip(),
            "llm_response": "",
            "user_id": user_name,
            "lang_code": lang_code
        }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

        llm_response = result.get("llm_response", "")
        error = result.get("error", "")

        if error:
            raise HTTPException(status_code=500, detail=error)

        chat_stored = add_to_conversation(
            user_name,
            human_message=query_text.strip(),
            ai_message=llm_response
        )

        return JSONResponse({
            "success": True,
            "message": f"Query processed and added to chat history for {user_name}",
            "user_name": user_name,
            "chat_history_updated": chat_stored,
            "transcribed_text": query_text.strip(),
            "response": llm_response,
            "storage_info": f"Added to Neo4j chat memory for: {user_name}"
        })

    except Exception as e:
        print(f"Error in handle_query: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/user/{user_name}")
async def get_user_info(user_name: str):
    if user_name not in registered_users:
        raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

    profile = get_user_profile(user_name)

    history = get_conversation_history(user_name)

    return {
        "success": True,
        "user": profile,
        "session_id": user_sessions.get(user_name),
        "total_messages": len(history),
        "recent_history": history[-10:] if history else [],
        "storage_info": f"Profile and chat history from Neo4j for: {user_name}"
    }

@app.get("/user/{user_name}/history")
async def get_complete_chat_history(user_name: str, limit: int = 100):
    if user_name not in registered_users:
        raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

    history = get_conversation_history(user_name)

    return {
        "success": True,
        "user_name": user_name,
        "session_id": user_sessions.get(user_name),
        "total_messages": len(history),
        "chat_history": history[-limit:] if history else [],
        "storage_info": f"Complete chat history from Neo4j for: {user_name}"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    
# from fastapi import FastAPI, File, UploadFile, HTTPException, Form
# from fastapi.responses import JSONResponse
# from fastapi.middleware.cors import CORSMiddleware
# from dotenv import load_dotenv
# import os
# import openai
# import google.generativeai as genai
# import base64
# from typing import TypedDict, Dict, Any, Optional
# from langgraph.graph import StateGraph
# import asyncio
# import io
# import json
# from pydantic import BaseModel
# import hashlib
# from datetime import datetime
# import traceback
# from uuid import uuid4
# from neo4j import GraphDatabase

# from langchain_neo4j import Neo4jChatMessageHistory, Neo4jGraph
# from langchain_core.messages import HumanMessage, AIMessage

# load_dotenv()

# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# if not OPENAI_API_KEY:
#     raise RuntimeError("OPENAI_API_KEY must be set in your .env file.")
# if not GEMINI_API_KEY:
#     raise RuntimeError("GEMINI_API_KEY must be set in your .env file.")

# openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
# genai.configure(api_key=GEMINI_API_KEY)


# neo4j_graph = Neo4jGraph(
#     url="bolt://localhost:7687",
#     username="neo4j",
#     password="Telebot1234"
# )

# neo4j_driver = GraphDatabase.driver(
#     "bolt://localhost:7687",
#     auth=("neo4j", "Telebot1234")
# )

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# class UserRegistration(BaseModel):
#     user_name: str
#     age: int = None
#     district: str = None
#     crops_grown: str = None
#     farm_size: str = None
#     contact: str = None


# registered_users = {}
# user_sessions = {}

# class TextQueryRequest(BaseModel):
#     user_name: str
#     query_text: str
#     lang_code: str

# @app.post("/text_query")
# async def handle_text_query(request: TextQueryRequest):
#     user_name = request.user_name
#     query_text = request.query_text.strip()
#     lang_code = request.lang_code

#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     if not query_text:
#         raise HTTPException(status_code=400, detail="Query text cannot be empty.")

#     try:
#         initial_state: QueryState = {
#             "query_text": query_text,
#             "llm_response": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

#         llm_response = result.get("llm_response", "Sorry, I could not process that.")

#         add_to_conversation(
#             user_name,
#             human_message=query_text,
#             ai_message=llm_response
#         )

#         return JSONResponse({
#             "success": True,
#             "transcribed_text": query_text,
#             "response": llm_response
#         })
#     except Exception as e:
#         print(f"Error in handle_text_query: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# def get_or_create_session_id(user_name: str) -> str:
#     """Get existing session ID or create new one for user"""
#     if user_name not in user_sessions:
#         user_sessions[user_name] = str(uuid4())
#         print(f"Created session ID {user_sessions[user_name]} for user {user_name}")
#     return user_sessions[user_name]

# def get_chat_memory(user_name: str) -> Neo4jChatMessageHistory:
#     """Get Neo4j chat memory for specific user"""
#     session_id = get_or_create_session_id(user_name)
#     return Neo4jChatMessageHistory(
#         session_id=session_id,
#         graph=neo4j_graph
#     )

# def store_user_profile(user_id: str, profile_data: Dict[str, Any]):
#     """Store user profile in Neo4j"""
#     try:
#         with neo4j_driver.session() as session:
#             session.run("""
#                 MERGE (u:UserProfile {user_id: $user_id})
#                 SET u.user_name = $user_name,
#                     u.age = $age,
#                     u.district = $district,
#                     u.crops_grown = $crops_grown,
#                     u.farm_size = $farm_size,
#                     u.contact = $contact,
#                     u.registration_date = $registration_date,
#                     u.session_id = $session_id,
#                     u.last_updated = $last_updated
#                 RETURN u
#             """,
#                 user_id=user_id,
#                 user_name=profile_data.get("user_name"),
#                 age=profile_data.get("age"),
#                 district=profile_data.get("district"),
#                 crops_grown=profile_data.get("crops_grown"),
#                 farm_size=profile_data.get("farm_size"),
#                 contact=profile_data.get("contact"),
#                 registration_date=profile_data.get("registration_date"),
#                 session_id=get_or_create_session_id(user_id),
#                 last_updated=datetime.now().isoformat()
#             )
#         return True
#     except Exception as e:
#         print(f"Error storing user profile: {e}")
#         return False

# def get_user_profile(user_id: str) -> Dict[str, Any]:
#     """Get user profile from Neo4j"""
#     try:
#         with neo4j_driver.session() as session:
#             result = session.run("""
#                 MATCH (u:UserProfile {user_id: $user_id})
#                 RETURN u.user_name as user_name,
#                        u.age as age,
#                        u.district as district,
#                        u.crops_grown as crops_grown,
#                        u.farm_size as farm_size,
#                        u.contact as contact,
#                        u.registration_date as registration_date,
#                        u.session_id as session_id,
#                        u.last_updated as last_updated
#             """, user_id=user_id)

#             record = result.single()
#             if record:
#                 if record["session_id"]:
#                     user_sessions[user_id] = record["session_id"]

#                 return {
#                     "user_id": user_id,
#                     "user_name": record["user_name"],
#                     "age": record["age"],
#                     "district": record["district"],
#                     "crops_grown": record["crops_grown"],
#                     "farm_size": record["farm_size"],
#                     "contact": record["contact"],
#                     "registration_date": record["registration_date"],
#                     "session_id": record["session_id"],
#                     "last_updated": record["last_updated"]
#                 }
#             return None
#     except Exception as e:
#         print(f"Error getting user profile: {e}")
#         return None

# def get_conversation_history(user_name: str) -> list:
#     """Get conversation history using LangChain Neo4j memory"""
#     try:
#         chat_memory = get_chat_memory(user_name)
#         messages = chat_memory.messages

#         history = []
#         for msg in messages:
#             if isinstance(msg, HumanMessage):
#                 history.append({
#                     "type": "human",
#                     "content": msg.content,
#                     "timestamp": datetime.now().isoformat()
#                 })
#             elif isinstance(msg, AIMessage):
#                 history.append({
#                     "type": "ai",
#                     "content": msg.content,
#                     "timestamp": datetime.now().isoformat()
#                 })

#         return history
#     except Exception as e:
#         print(f"Error getting conversation history: {e}")
#         return []

# def add_to_conversation(user_name: str, human_message: str = None, ai_message: str = None):
#     """Add message to conversation history"""
#     try:
#         chat_memory = get_chat_memory(user_name)

#         if human_message:
#             chat_memory.add_user_message(human_message)
#             print(f" Added human message for {user_name}")

#         if ai_message:
#             chat_memory.add_ai_message(ai_message)
#             print(f"Added AI message for {user_name}")

#         return True
#     except Exception as e:
#         print(f"Error adding to conversation: {e}")
#         return False

# def get_relevant_context(user_name: str, query: str) -> str:
#     """Get relevant context from user's conversation history"""
#     try:
#         history = get_conversation_history(user_name)

#         profile = get_user_profile(user_name)
#         context_parts = []

#         if profile:
#             profile_context = f"User: {profile.get('user_name')} from {profile.get('district', 'Unknown')} grows {profile.get('crops_grown', 'various crops')}"
#             context_parts.append(profile_context)

#         for msg in history[-6:]:
#             if msg["type"] == "human":
#                 context_parts.append(f"User asked: {msg['content'][:150]}...")
#             elif msg["type"] == "ai":
#                 context_parts.append(f"AI responded: {msg['content'][:150]}...")

#         return "\n".join(context_parts) if context_parts else ""

#     except Exception as e:
#         print(f"Error getting relevant context: {e}")
#         return ""

# class PestState(TypedDict):
#     image_b64: str
#     description: str
#     diagnosis: str
#     user_id: str
#     lang_code: str

# class QueryState(TypedDict):
#     query_text: str
#     llm_response: str
#     user_id: str
#     lang_code: str

# def describe_leaf(state: PestState) -> Dict[str, Any]:
#     if "image_b64" not in state or not state["image_b64"]:
#         return {"error": "No image found in input."}

#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     prompt = (
#         f"""You are a senior plant pathologist with 15 years of experience specializing in tropical crop diseases, particularly those affecting Kerala's major agricultural crops including coconut, rubber, pepper, cardamom, rice, banana, tea, coffee, ginger, turmeric, and spices.
#         ANALYSIS INSTRUCTIONS:
#         Carefully examine this leaf image and provide detailed description focusing on:

#         1. VISUAL SYMPTOMS:
#         - Leaf discoloration patterns (yellowing, browning, blackening, reddening)
#         - Spot characteristics (size, shape, color, borders, concentric rings)
#         - Physical damage (holes, wilting, curling, distortion)
#         - Surface abnormalities (powdery coating, fuzzy growth, sticky residue)
#         - Vein patterns and leaf texture changes

#         2. DISTRIBUTION PATTERN:
#         - Location of symptoms (leaf tips, edges, center, base)
#         - Progression pattern (scattered, clustered, systematic)
#         - Age-related symptoms (young vs. old leaves)

#         3. ENVIRONMENTAL CONTEXT:
#         - Consider Kerala's tropical monsoon climate
#         - High humidity and temperature effects
#         - Seasonal disease patterns common in the region

#         Keep the entire response short and to the point.
#         Describe ONLY what you observe - avoid making diagnostic conclusions at this stage.  Use precise botanical and pathological terminology. Consider that this may be from crops commonly grown in Kerala's diverse agro-climatic zones. 
#         {language_instruction}"""
#     )

#     messages = [
#         {
#             "role": "user",
#             "content": [
#                 {"type": "text", "text": prompt},
#                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{state['image_b64']}"}}
#             ]
#         }
#     ]

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=messages,
#             max_tokens=800
#         )
#         desc = response.choices[0].message.content or ""
#         return {"description": desc, "user_id": user_id, "lang_code": lang_code}
#     except Exception as e:
#         print(f"ERROR in describe_leaf: {e}")
#         return {"error": f"Vision model error: {str(e)}", "user_id": user_id}

# def diagnose_leaf(state: PestState) -> Dict[str, Any]:
#     description = state.get("description", "")
#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     if not description:
#         return {"error": "No description provided for diagnosis.", "user_id": user_id}

#     context = get_relevant_context(user_id, description)
#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     memory_context = ""
#     if context:
#         memory_context = f"""
#         Here is some context about the user and recent conversation:
#         {context}
#         Use this to tailor your advice.
#         """
#     # MODIFIED: New, more concise prompt for diagnosis
#     prompt = (
#         f"""You are a senior plant pathology expert for Kerala crops. Analyze the following leaf description.

#         {memory_context}

#         Leaf Description: {description}

#         Instructions:
#         1.  Identify: State the most likely disease/pest concisely.
#         2.  Summarize: Provide a brief, 2-3 sentence summary of the issue.
#         3.  Recommend Actions: List the top 2-3 most critical treatment or prevention steps as bullet points.
#         4.  Offer More Info: Conclude by asking "Would you like a more detailed explanation on the diagnosis, treatments, or prevention?"

#         Keep the entire response short and to the point.
#         {language_instruction}
#         """
#     )

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=[{"role": "user", "content": prompt}],
#             max_tokens=1000
#         )
#         diag = response.choices[0].message.content or ""

#         return {"diagnosis": diag, "user_id": user_id}
#     except Exception as e:
#         print(f"ERROR in diagnose_leaf: {e}")
#         return {"error": f"Diagnosis model error: {str(e)}", "user_id": user_id}

# def process_query(state: QueryState) -> Dict[str, Any]:
#     query_text = state.get("query_text", "")
#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     if not query_text:
#         return {"error": "No query text provided.", "user_id": user_id}

#     context = get_relevant_context(user_id, query_text)
#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     memory_context = ""
#     if context:
#         memory_context = f"""
#         Here is some context about the user and recent conversation:
#         {context}
#         Use this to provide informed agricultural advice.
#         """

#     # MODIFIED: New, more concise prompt for general queries
#     prompt = (
#         f"""You are a top agricultural expert for Kerala. A farmer has the following query.

#         {memory_context}

#         Farmer's Query: {query_text}

#         Instructions:
#         1.  Direct Answer: Provide a short, direct answer to the farmer's main question.
#         2.  Key Actions: List the 2-3 most important actions the farmer should take as bullet points.
#         3.  Offer More Info:Conclude by asking "Let me know if you need more details on any of these points."

#         Be concise, practical, and focus on actionable advice. Avoid long paragraphs.
#         {language_instruction}
#         """
#     )

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=[{"role": "user", "content": prompt}],
#             max_tokens=1000
#         )
#         answer = response.choices[0].message.content or ""

#         return {"llm_response": answer, "user_id": user_id}
#     except Exception as e:
#         print(f"ERROR in process_query: {e}")
#         return {"error": f"Query processing error: {str(e)}", "user_id": user_id}

# async def transcribe_audio_with_gemini(audio_data: bytes, filename: str) -> str:
#     try:
#         model = genai.GenerativeModel('gemini-1.5-flash')

#         audio_file = {
#             "mime_type": "audio/wav",
#             "data": audio_data
#         }

#         prompt = (
#             "Please transcribe the following audio file. The audio may contain speech in Malayalam, English, or other languages. "
#             "Provide the transcription in the original language spoken. If Malayalam is detected, provide transcription in Malayalam script. "
#             "Focus on agricultural, farming, or plant-related content."
#         )

#         response = model.generate_content([prompt, audio_file])

#         if response.text:
#             return response.text.strip()
#         else:
#             raise Exception("No transcription returned from Gemini")

#     except Exception as e:
#         print(f"Gemini transcription error: {e}")
#         raise Exception(f"Gemini transcription failed: {str(e)}")

# def create_pest_workflow():
#     pest_graph = StateGraph(PestState)
#     pest_graph.add_node("describe_leaf", describe_leaf)
#     pest_graph.add_node("diagnose_leaf", diagnose_leaf)
#     pest_graph.add_edge("describe_leaf", "diagnose_leaf")
#     pest_graph.set_entry_point("describe_leaf")
#     pest_graph.set_finish_point("diagnose_leaf")
#     return pest_graph.compile()

# def create_query_workflow():
#     query_graph = StateGraph(QueryState)
#     query_graph.add_node("process_query", process_query)
#     query_graph.set_entry_point("process_query")
#     query_graph.set_finish_point("process_query")
#     return query_graph.compile()

# pest_flow = create_pest_workflow()
# query_flow = create_query_workflow()

# @app.get("/")
# def root():
#     return {
#         "message": "Plant Disease Diagnosis API with LangChain Neo4j Chat Memory",
#         "endpoints": {
#             "/register": "POST - Register user",
#             "/analyze": "POST - Upload leaf image",
#             "/query": "POST - Upload audio file",
#             "/text_query": "POST - Submit a text query",
#             "/user/{user_name}": "GET - Get user info",
#             "/user/{user_name}/history": "GET - Get chat history"
#         },
#         "features": {
#             "langchain_memory": "Uses Neo4jChatMessageHistory for conversation persistence",
#             "session_management": "Each user gets unique session ID",
#             "graph_storage": "Messages stored as connected nodes with NEXT relationships"
#         },
#         "status": "Running with LangChain Neo4j Chat Memory"
#     }

# @app.post("/register")
# async def register_user(user_data: UserRegistration):
#     try:
#         user_name = user_data.user_name.strip()
#         if not user_name:
#             raise HTTPException(status_code=400, detail="User name cannot be empty")

#         user_id = user_name

#         user_info = {
#             "user_id": user_id,
#             "user_name": user_name,
#             "age": user_data.age,
#             "district": user_data.district,
#             "crops_grown": user_data.crops_grown,
#             "farm_size": user_data.farm_size,
#             "contact": user_data.contact,
#             "registration_date": datetime.now().isoformat()
#         }

#         registered_users[user_id] = user_info

#         profile_stored = store_user_profile(user_id, user_info)

#         session_id = get_or_create_session_id(user_name)
#         registration_msg = f"User {user_name} registered from {user_data.district or 'Unknown district'}"
#         chat_stored = add_to_conversation(user_name, human_message=registration_msg)

#         return {
#             "success": True,
#             "message": f"User {user_name} registered with chat memory",
#             "user_id": user_id,
#             "user_name": user_name,
#             "session_id": session_id,
#             "profile_stored": profile_stored,
#             "chat_history_started": chat_stored,
#             "storage_info": f"Profile and chat history stored in Neo4j for: {user_name}"
#         }

#     except Exception as e:
#         print(f"Registration error: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

# @app.post("/analyze")
# async def analyze_leaf(user_name: str = Form(...), lang_code: str = Form(...), file: UploadFile = File(...)):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     allowed_types = ["image/jpeg", "image/png", "image/jpg"]
#     if file.content_type not in allowed_types:
#         raise HTTPException(status_code=400, detail=f"Only JPEG and PNG images supported.")

#     try:
#         contents = await file.read()
#         if not contents:
#             raise HTTPException(status_code=400, detail="Uploaded file is empty.")

#         b64_image = base64.b64encode(contents).decode('utf-8')

#         initial_state: PestState = {
#             "image_b64": b64_image,
#             "description": "",
#             "diagnosis": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, pest_flow.invoke, initial_state)

#         description = result.get("description", "")
#         diagnosis = result.get("diagnosis", "")
#         error = result.get("error", "")

#         if error:
#             raise HTTPException(status_code=500, detail=error)

#         human_msg = f"Analyzed leaf image: {file.filename}"
#         ai_msg = f"Description: {description[:100]}... Diagnosis: {diagnosis[:100]}..."

#         chat_stored = add_to_conversation(
#             user_name,
#             human_message=human_msg,
#             ai_message=ai_msg
#         )

#         return JSONResponse({
#             "success": True,
#             "message": f"Analysis completed and added to chat history for {user_name}",
#             "user_name": user_name,
#             "chat_history_updated": chat_stored,
#             "description": description,
#             "diagnosis": diagnosis,
#             "storage_info": f"Added to Neo4j chat memory for: {user_name}"
#         })

#     except Exception as e:
#         print(f"Error in analyze_leaf: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# @app.post("/query")
# async def handle_query(user_name: str = Form(...), audio: UploadFile = File(...)):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     try:
#         audio_contents = await audio.read()
#         if not audio_contents:
#             raise HTTPException(status_code=400, detail="Uploaded audio file is empty.")

#         query_text = await transcribe_audio_with_gemini(audio_contents, audio.filename or "audio.wav")

#         if not query_text or query_text.strip() == "":
#             raise HTTPException(status_code=400, detail="Could not transcribe audio.")

#         lang_code = "ml"

#         initial_state: QueryState = {
#             "query_text": query_text.strip(),
#             "llm_response": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

#         llm_response = result.get("llm_response", "")
#         error = result.get("error", "")

#         if error:
#             raise HTTPException(status_code=500, detail=error)

#         chat_stored = add_to_conversation(
#             user_name,
#             human_message=query_text.strip(),
#             ai_message=llm_response
#         )

#         return JSONResponse({
#             "success": True,
#             "message": f"Query processed and added to chat history for {user_name}",
#             "user_name": user_name,
#             "chat_history_updated": chat_stored,
#             "transcribed_text": query_text.strip(),
#             "response": llm_response,
#             "storage_info": f"Added to Neo4j chat memory for: {user_name}"
#         })

#     except Exception as e:
#         print(f"Error in handle_query: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# @app.get("/user/{user_name}")
# async def get_user_info(user_name: str):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

#     profile = get_user_profile(user_name)

#     history = get_conversation_history(user_name)

#     return {
#         "success": True,
#         "user": profile,
#         "session_id": user_sessions.get(user_name),
#         "total_messages": len(history),
#         "recent_history": history[-10:] if history else [],
#         "storage_info": f"Profile and chat history from Neo4j for: {user_name}"
#     }

# @app.get("/user/{user_name}/history")
# async def get_complete_chat_history(user_name: str, limit: int = 100):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

#     history = get_conversation_history(user_name)

#     return {
#         "success": True,
#         "user_name": user_name,
#         "session_id": user_sessions.get(user_name),
#         "total_messages": len(history),
#         "chat_history": history[-limit:] if history else [],
#         "storage_info": f"Complete chat history from Neo4j for: {user_name}"
#     }

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)



# from fastapi import FastAPI, File, UploadFile, HTTPException, Form
# from fastapi.responses import JSONResponse
# from fastapi.middleware.cors import CORSMiddleware
# from dotenv import load_dotenv
# import os
# import openai
# import google.generativeai as genai
# import base64
# from typing import TypedDict, Dict, Any, Optional
# from langgraph.graph import StateGraph
# import asyncio
# import io
# import json
# from pydantic import BaseModel
# import hashlib
# from datetime import datetime
# import traceback
# from uuid import uuid4
# from neo4j import GraphDatabase

# from langchain_neo4j import Neo4jChatMessageHistory, Neo4jGraph
# from langchain_core.messages import HumanMessage, AIMessage

# load_dotenv()

# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# if not OPENAI_API_KEY:
#     raise RuntimeError("OPENAI_API_KEY must be set in your .env file.")
# if not GEMINI_API_KEY:
#     raise RuntimeError("GEMINI_API_KEY must be set in your .env file.")

# openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
# genai.configure(api_key=GEMINI_API_KEY)


# neo4j_graph = Neo4jGraph(
#     url="bolt://localhost:7687",
#     username="neo4j",
#     password="Telebot1234"
# )

# neo4j_driver = GraphDatabase.driver(
#     "bolt://localhost:7687",
#     auth=("neo4j", "Telebot1234")
# )

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# class UserRegistration(BaseModel):
#     user_name: str
#     age: int = None
#     district: str = None
#     crops_grown: str = None
#     farm_size: str = None
#     contact: str = None


# registered_users = {}
# user_sessions = {}

# class TextQueryRequest(BaseModel):
#     user_name: str
#     query_text: str
#     lang_code: str

# @app.post("/query_text")
# async def handle_text_query(request: TextQueryRequest):
#     user_name = request.user_name
#     query_text = request.query_text.strip()
#     lang_code = request.lang_code

#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     if not query_text:
#         raise HTTPException(status_code=400, detail="Query text cannot be empty.")

#     try:
#         initial_state: QueryState = {
#             "query_text": query_text,
#             "llm_response": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

#         llm_response = result.get("llm_response", "Sorry, I could not process that.")

#         add_to_conversation(
#             user_name,
#             human_message=query_text,
#             ai_message=llm_response
#         )

#         return JSONResponse({
#             "success": True,
#             "transcribed_text": query_text,
#             "response": llm_response
#         })
#     except Exception as e:
#         print(f"Error in handle_text_query: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# def get_or_create_session_id(user_name: str) -> str:
#     """Get existing session ID or create new one for user"""
#     if user_name not in user_sessions:
#         user_sessions[user_name] = str(uuid4())
#         print(f"Created session ID {user_sessions[user_name]} for user {user_name}")
#     return user_sessions[user_name]

# def get_chat_memory(user_name: str) -> Neo4jChatMessageHistory:
#     """Get Neo4j chat memory for specific user"""
#     session_id = get_or_create_session_id(user_name)
#     return Neo4jChatMessageHistory(
#         session_id=session_id,
#         graph=neo4j_graph
#     )

# def store_user_profile(user_id: str, profile_data: Dict[str, Any]):
#     """Store user profile in Neo4j"""
#     try:
#         with neo4j_driver.session() as session:
#             session.run("""
#                 MERGE (u:UserProfile {user_id: $user_id})
#                 SET u.user_name = $user_name,
#                     u.age = $age,
#                     u.district = $district,
#                     u.crops_grown = $crops_grown,
#                     u.farm_size = $farm_size,
#                     u.contact = $contact,
#                     u.registration_date = $registration_date,
#                     u.session_id = $session_id,
#                     u.last_updated = $last_updated
#                 RETURN u
#             """,
#                 user_id=user_id,
#                 user_name=profile_data.get("user_name"),
#                 age=profile_data.get("age"),
#                 district=profile_data.get("district"),
#                 crops_grown=profile_data.get("crops_grown"),
#                 farm_size=profile_data.get("farm_size"),
#                 contact=profile_data.get("contact"),
#                 registration_date=profile_data.get("registration_date"),
#                 session_id=get_or_create_session_id(user_id),
#                 last_updated=datetime.now().isoformat()
#             )
#         return True
#     except Exception as e:
#         print(f"Error storing user profile: {e}")
#         return False

# def get_user_profile(user_id: str) -> Dict[str, Any]:
#     """Get user profile from Neo4j"""
#     try:
#         with neo4j_driver.session() as session:
#             result = session.run("""
#                 MATCH (u:UserProfile {user_id: $user_id})
#                 RETURN u.user_name as user_name,
#                        u.age as age,
#                        u.district as district,
#                        u.crops_grown as crops_grown,
#                        u.farm_size as farm_size,
#                        u.contact as contact,
#                        u.registration_date as registration_date,
#                        u.session_id as session_id,
#                        u.last_updated as last_updated
#             """, user_id=user_id)

#             record = result.single()
#             if record:
#                 if record["session_id"]:
#                     user_sessions[user_id] = record["session_id"]

#                 return {
#                     "user_id": user_id,
#                     "user_name": record["user_name"],
#                     "age": record["age"],
#                     "district": record["district"],
#                     "crops_grown": record["crops_grown"],
#                     "farm_size": record["farm_size"],
#                     "contact": record["contact"],
#                     "registration_date": record["registration_date"],
#                     "session_id": record["session_id"],
#                     "last_updated": record["last_updated"]
#                 }
#             return None
#     except Exception as e:
#         print(f"Error getting user profile: {e}")
#         return None

# def get_conversation_history(user_name: str) -> list:
#     """Get conversation history using LangChain Neo4j memory"""
#     try:
#         chat_memory = get_chat_memory(user_name)
#         messages = chat_memory.messages

#         history = []
#         for msg in messages:
#             if isinstance(msg, HumanMessage):
#                 history.append({
#                     "type": "human",
#                     "content": msg.content,
#                     "timestamp": datetime.now().isoformat()
#                 })
#             elif isinstance(msg, AIMessage):
#                 history.append({
#                     "type": "ai",
#                     "content": msg.content,
#                     "timestamp": datetime.now().isoformat()
#                 })

#         return history
#     except Exception as e:
#         print(f"Error getting conversation history: {e}")
#         return []

# def add_to_conversation(user_name: str, human_message: str = None, ai_message: str = None):
#     """Add message to conversation history"""
#     try:
#         chat_memory = get_chat_memory(user_name)

#         if human_message:
#             chat_memory.add_user_message(human_message)
#             print(f" Added human message for {user_name}")

#         if ai_message:
#             chat_memory.add_ai_message(ai_message)
#             print(f"Added AI message for {user_name}")

#         return True
#     except Exception as e:
#         print(f"Error adding to conversation: {e}")
#         return False

# def get_relevant_context(user_name: str, query: str) -> str:
#     """Get relevant context from user's conversation history"""
#     try:
#         history = get_conversation_history(user_name)

#         profile = get_user_profile(user_name)
#         context_parts = []

#         if profile:
#             profile_context = f"User: {profile.get('user_name')} from {profile.get('district', 'Unknown')} grows {profile.get('crops_grown', 'various crops')}"
#             context_parts.append(profile_context)

#         for msg in history[-6:]:
#             if msg["type"] == "human":
#                 context_parts.append(f"User asked: {msg['content'][:150]}...")
#             elif msg["type"] == "ai":
#                 context_parts.append(f"AI responded: {msg['content'][:150]}...")

#         return "\n".join(context_parts) if context_parts else ""

#     except Exception as e:
#         print(f"Error getting relevant context: {e}")
#         return ""

# class PestState(TypedDict):
#     image_b64: str
#     description: str
#     diagnosis: str
#     user_id: str
#     lang_code: str

# class QueryState(TypedDict):
#     query_text: str
#     llm_response: str
#     user_id: str
#     lang_code: str

# def describe_leaf(state: PestState) -> Dict[str, Any]:
#     if "image_b64" not in state or not state["image_b64"]:
#         return {"error": "No image found in input."}

#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     prompt = (
#         f"""You are a senior plant pathologist with 15 years of experience specializing in tropical crop diseases, particularly those affecting Kerala's major agricultural crops including coconut, rubber, pepper, cardamom, rice, banana, tea, coffee, ginger, turmeric, and spices.
#         ANALYSIS INSTRUCTIONS:
#         Carefully examine this leaf image and provide a detailed description focusing on:

#         1. VISUAL SYMPTOMS:
#         - Leaf discoloration patterns (yellowing, browning, blackening, reddening)
#         - Spot characteristics (size, shape, color, borders, concentric rings)
#         - Physical damage (holes, wilting, curling, distortion)
#         - Surface abnormalities (powdery coating, fuzzy growth, sticky residue)
#         - Vein patterns and leaf texture changes

#         2. DISTRIBUTION PATTERN:
#         - Location of symptoms (leaf tips, edges, center, base)
#         - Progression pattern (scattered, clustered, systematic)
#         - Age-related symptoms (young vs. old leaves)

#         3. ENVIRONMENTAL CONTEXT:
#         - Consider Kerala's tropical monsoon climate
#         - High humidity and temperature effects
#         - Seasonal disease patterns common in the region

#         Describe ONLY what you observe - avoid making diagnostic conclusions at this stage. Use precise botanical and pathological terminology. Consider that this may be from crops commonly grown in Kerala's diverse agro-climatic zones.
#         {language_instruction}"""
#     )

#     messages = [
#         {
#             "role": "user",
#             "content": [
#                 {"type": "text", "text": prompt},
#                 {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{state['image_b64']}"}}
#             ]
#         }
#     ]

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=messages,
#             max_tokens=800
#         )
#         desc = response.choices[0].message.content or ""
#         return {"description": desc, "user_id": user_id, "lang_code": lang_code}
#     except Exception as e:
#         print(f"ERROR in describe_leaf: {e}")
#         return {"error": f"Vision model error: {str(e)}", "user_id": user_id}

# def diagnose_leaf(state: PestState) -> Dict[str, Any]:
#     description = state.get("description", "")
#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     if not description:
#         return {"error": "No description provided for diagnosis.", "user_id": user_id}

#     context = get_relevant_context(user_id, description)
#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     memory_context = ""
#     if context:
#         memory_context = f"""
#         Your profile and conversation history:
#         {context}

#         Use this context to provide personalized advice:
#         """

#     prompt = (
#         f"""You are a senior plant pathology expert specializing in tropical crop diseases, particularly those affecting Kerala's major agricultural crops including coconut, rubber, pepper, cardamom, rice, banana, tea, coffee, ginger, turmeric, and spices.

#         {memory_context}

#         Given the following description of a plant leaf, provide a comprehensive analysis:
#         1. What symptoms are visible?
#         2. What is the most likely disease or condition?
#         3. What pest or pathogen might cause it?
#         4. What are recommended organic treatments?
#         5. What are recommended chemical treatments?
#         6. What preventive measures should be taken by the farmer?

#         leaf description: {description}
#         {language_instruction}
#         """
#     )

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=[{"role": "user", "content": prompt}],
#             max_tokens=1000
#         )
#         diag = response.choices[0].message.content or ""

#         return {"diagnosis": diag, "user_id": user_id}
#     except Exception as e:
#         print(f"ERROR in diagnose_leaf: {e}")
#         return {"error": f"Diagnosis model error: {str(e)}", "user_id": user_id}

# def process_query(state: QueryState) -> Dict[str, Any]:
#     query_text = state.get("query_text", "")
#     user_id = state.get("user_id", "farmer_system")
#     lang_code = state.get("lang_code", "en")

#     if not query_text:
#         return {"error": "No query text provided.", "user_id": user_id}

#     context = get_relevant_context(user_id, query_text)
#     language_instruction = "give response in malayalam only" if lang_code == 'ml' else "give response in english only"

#     memory_context = ""
#     if context:
#         memory_context = f"""
#         Your profile and conversation history:
#         {context}

#         Use this context to provide informed agricultural advice:
#         """

#     prompt = (
#         f"""You are Kerala's leading agricultural consultant and plant pathologist with 15+ years of field experience. You've worked extensively with farmers across Kerala's diverse agro-climatic zones - from the coastal plains to the Western Ghats. Your expertise covers all major Kerala crops and you understand the unique challenges posed by the state's tropical monsoon climate.

#         {memory_context}

#         KERALA AGRICULTURAL CONTEXT:
#         Major Crops: Coconut, rubber, pepper, cardamom, rice, banana, tea, coffee, ginger, turmeric, vegetables
#         Climate: Tropical monsoon with heavy rains (June-September), high humidity year-round
#         Regions: Coastal plains, midlands, high ranges - each with specific challenges
#         Farmers: Mix of traditional knowledge and modern techniques

#         RESPONSE GUIDELINES:
#         - Provide practical, actionable advice suitable for Kerala's climate
#         - Include both modern scientific methods and traditional Kerala practices when relevant
#         - Consider monsoon timing in your recommendations
#         - Mention locally available materials and resources
#         - Address economic aspects - cost-effective solutions for small farmers
#         - Include preventive measures specific to high humidity conditions
#         - Reference Kerala's agricultural calendar when timing is important

#         FARMER'S QUERY:
#         {query_text}

#         Provide a comprehensive, empathetic response as if you're directly consulting with a Kerala farmer. Structure your answer with clear sections and practical steps they can implement immediately.
#         {language_instruction}
#         """
#     )

#     try:
#         response = openai_client.chat.completions.create(
#             model="gpt-4o",
#             messages=[{"role": "user", "content": prompt}],
#             max_tokens=1000
#         )
#         answer = response.choices[0].message.content or ""

#         return {"llm_response": answer, "user_id": user_id}
#     except Exception as e:
#         print(f"ERROR in process_query: {e}")
#         return {"error": f"Query processing error: {str(e)}", "user_id": user_id}

# async def transcribe_audio_with_gemini(audio_data: bytes, filename: str) -> str:
#     try:
#         model = genai.GenerativeModel('gemini-1.5-flash')

#         audio_file = {
#             "mime_type": "audio/wav",
#             "data": audio_data
#         }

#         prompt = (
#             "Please transcribe the following audio file. The audio may contain speech in Malayalam, English, or other languages. "
#             "Provide the transcription in the original language spoken. If Malayalam is detected, provide transcription in Malayalam script. "
#             "Focus on agricultural, farming, or plant-related content."
#         )

#         response = model.generate_content([prompt, audio_file])

#         if response.text:
#             return response.text.strip()
#         else:
#             raise Exception("No transcription returned from Gemini")

#     except Exception as e:
#         print(f"Gemini transcription error: {e}")
#         raise Exception(f"Gemini transcription failed: {str(e)}")

# def create_pest_workflow():
#     pest_graph = StateGraph(PestState)
#     pest_graph.add_node("describe_leaf", describe_leaf)
#     pest_graph.add_node("diagnose_leaf", diagnose_leaf)
#     pest_graph.add_edge("describe_leaf", "diagnose_leaf")
#     pest_graph.set_entry_point("describe_leaf")
#     pest_graph.set_finish_point("diagnose_leaf")
#     return pest_graph.compile()

# def create_query_workflow():
#     query_graph = StateGraph(QueryState)
#     query_graph.add_node("process_query", process_query)
#     query_graph.set_entry_point("process_query")
#     query_graph.set_finish_point("process_query")
#     return query_graph.compile()

# pest_flow = create_pest_workflow()
# query_flow = create_query_workflow()

# @app.get("/")
# def root():
#     return {
#         "message": "Plant Disease Diagnosis API with LangChain Neo4j Chat Memory",
#         "endpoints": {
#             "/register": "POST - Register user",
#             "/analyze": "POST - Upload leaf image",
#             "/query": "POST - Upload audio file",
#             "/user/{user_name}": "GET - Get user info",
#             "/user/{user_name}/history": "GET - Get chat history"
#         },
#         "features": {
#             "langchain_memory": "Uses Neo4jChatMessageHistory for conversation persistence",
#             "session_management": "Each user gets unique session ID",
#             "graph_storage": "Messages stored as connected nodes with NEXT relationships"
#         },
#         "status": "Running with LangChain Neo4j Chat Memory"
#     }

# @app.post("/register")
# async def register_user(user_data: UserRegistration):
#     try:
#         user_name = user_data.user_name.strip()
#         if not user_name:
#             raise HTTPException(status_code=400, detail="User name cannot be empty")

#         user_id = user_name

#         user_info = {
#             "user_id": user_id,
#             "user_name": user_name,
#             "age": user_data.age,
#             "district": user_data.district,
#             "crops_grown": user_data.crops_grown,
#             "farm_size": user_data.farm_size,
#             "contact": user_data.contact,
#             "registration_date": datetime.now().isoformat()
#         }

#         registered_users[user_id] = user_info

#         profile_stored = store_user_profile(user_id, user_info)

#         session_id = get_or_create_session_id(user_name)
#         registration_msg = f"User {user_name} registered from {user_data.district or 'Unknown district'}"
#         chat_stored = add_to_conversation(user_name, human_message=registration_msg)

#         return {
#             "success": True,
#             "message": f"User {user_name} registered with chat memory",
#             "user_id": user_id,
#             "user_name": user_name,
#             "session_id": session_id,
#             "profile_stored": profile_stored,
#             "chat_history_started": chat_stored,
#             "storage_info": f"Profile and chat history stored in Neo4j for: {user_name}"
#         }

#     except Exception as e:
#         print(f"Registration error: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")

# @app.post("/analyze")
# async def analyze_leaf(user_name: str = Form(...), lang_code: str = Form(...), file: UploadFile = File(...)):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     allowed_types = ["image/jpeg", "image/png", "image/jpg"]
#     if file.content_type not in allowed_types:
#         raise HTTPException(status_code=400, detail=f"Only JPEG and PNG images supported.")

#     try:
#         contents = await file.read()
#         if not contents:
#             raise HTTPException(status_code=400, detail="Uploaded file is empty.")

#         b64_image = base64.b64encode(contents).decode('utf-8')

#         initial_state: PestState = {
#             "image_b64": b64_image,
#             "description": "",
#             "diagnosis": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, pest_flow.invoke, initial_state)

#         description = result.get("description", "")
#         diagnosis = result.get("diagnosis", "")
#         error = result.get("error", "")

#         if error:
#             raise HTTPException(status_code=500, detail=error)

#         human_msg = f"Analyzed leaf image: {file.filename}"
#         ai_msg = f"Description: {description[:100]}... Diagnosis: {diagnosis[:100]}..."

#         chat_stored = add_to_conversation(
#             user_name,
#             human_message=human_msg,
#             ai_message=ai_msg
#         )

#         return JSONResponse({
#             "success": True,
#             "message": f"Analysis completed and added to chat history for {user_name}",
#             "user_name": user_name,
#             "chat_history_updated": chat_stored,
#             "description": description,
#             "diagnosis": diagnosis,
#             "storage_info": f"Added to Neo4j chat memory for: {user_name}"
#         })

#     except Exception as e:
#         print(f"Error in analyze_leaf: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# @app.post("/query")
# async def handle_query(user_name: str = Form(...), audio: UploadFile = File(...)):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found.")

#     try:
#         audio_contents = await audio.read()
#         if not audio_contents:
#             raise HTTPException(status_code=400, detail="Uploaded audio file is empty.")

#         query_text = await transcribe_audio_with_gemini(audio_contents, audio.filename or "audio.wav")

#         if not query_text or query_text.strip() == "":
#             raise HTTPException(status_code=400, detail="Could not transcribe audio.")

#         # This endpoint is audio-only, so we can't easily get lang_code.
#         # For full multilingual support, the bot should use the /query_text endpoint.
#         # Defaulting to a sensible value, or you could try language detection.
#         lang_code = "ml" # Defaulting to Malayalam as per original prompts

#         initial_state: QueryState = {
#             "query_text": query_text.strip(),
#             "llm_response": "",
#             "user_id": user_name,
#             "lang_code": lang_code
#         }

#         loop = asyncio.get_event_loop()
#         result = await loop.run_in_executor(None, query_flow.invoke, initial_state)

#         llm_response = result.get("llm_response", "")
#         error = result.get("error", "")

#         if error:
#             raise HTTPException(status_code=500, detail=error)

#         chat_stored = add_to_conversation(
#             user_name,
#             human_message=query_text.strip(),
#             ai_message=llm_response
#         )

#         return JSONResponse({
#             "success": True,
#             "message": f"Query processed and added to chat history for {user_name}",
#             "user_name": user_name,
#             "chat_history_updated": chat_stored,
#             "transcribed_text": query_text.strip(),
#             "response": llm_response,
#             "storage_info": f"Added to Neo4j chat memory for: {user_name}"
#         })

#     except Exception as e:
#         print(f"Error in handle_query: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

# @app.get("/user/{user_name}")
# async def get_user_info(user_name: str):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

#     profile = get_user_profile(user_name)

#     history = get_conversation_history(user_name)

#     return {
#         "success": True,
#         "user": profile,
#         "session_id": user_sessions.get(user_name),
#         "total_messages": len(history),
#         "recent_history": history[-10:] if history else [],
#         "storage_info": f"Profile and chat history from Neo4j for: {user_name}"
#     }

# @app.get("/user/{user_name}/history")
# async def get_complete_chat_history(user_name: str, limit: int = 100):
#     if user_name not in registered_users:
#         raise HTTPException(status_code=404, detail=f"User '{user_name}' not found")

#     history = get_conversation_history(user_name)

#     return {
#         "success": True,
#         "user_name": user_name,
#         "session_id": user_sessions.get(user_name),
#         "total_messages": len(history),
#         "chat_history": history[-limit:] if history else [],
#         "storage_info": f"Complete chat history from Neo4j for: {user_name}"
#     }

# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)

