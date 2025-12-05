from langchain.agents import create_agent
from langchain_ollama import ChatOllama
import requests, json
import logging
from dateutil import parser
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from langchain.agents.middleware import ToolRetryMiddleware, SummarizationMiddleware
from langchain_community.utilities import SearxSearchWrapper
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from uuid import uuid4
import uvicorn
from langchain.tools import tool
from contextlib import asynccontextmanager
from toon_format import encode
import psycopg, re
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import asyncio

# --- Imports for LangGraph Memory ---
from langgraph.checkpoint.postgres import PostgresSaver
from langchain_core.messages import HumanMessage, AIMessage

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Database configuration
DB_URI = "postgresql://developer:password@localhost:5432/users"

# Initialize the chat model
model = ChatOllama(model="gpt-oss:20b-cloud", temperature=0)

# Connection pool for async operations
connection_pool = None
# Memory will be initialized in lifespan
memory = None

async def init_database():
    """Initialize database connection pool and create tables"""
    global connection_pool
    try:
        connection_pool = AsyncConnectionPool(
            conninfo=DB_URI,
            min_size=1,
            max_size=10,
            open=False
        )
        await connection_pool.open()
        await ensure_tables_exist()
        logger.info("Database connection pool initialized successfully")
        return connection_pool
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

async def ensure_tables_exist():
    """Creates the necessary tables for storing user info and job status if they don't exist."""
    try:
        async with connection_pool.connection() as conn:
            async with conn.cursor() as cur:
                # User profiles table
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        session_id TEXT PRIMARY KEY,
                        name TEXT,
                        location TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                # Job status table for async tasks
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS job_status (
                        session_id TEXT PRIMARY KEY,
                        cid TEXT,
                        status VARCHAR(20) NOT NULL,
                        ai_response TEXT,
                        error TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)
        logger.info("Database tables verified.")
    except Exception as e:
        logger.error(f"Error ensuring tables exist: {e}")
        raise

async def get_user_profile(session_id: str):
    """Fetch user info from the database by session_id."""
    if not connection_pool:
        return None

    try:
        async with connection_pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT name, location FROM user_profiles WHERE session_id = %s",
                    (session_id,)
                )
                row = await cur.fetchone()
                if row:
                    return {"name": row["name"], "location": row["location"]}
                return None
    except Exception as e:
        logger.error(f"Error fetching user profile: {e}")
        return None

# Session storage for non-persistent data
sessions: Dict[str, Dict] = {}
SESSION_TIMEOUT = timedelta(hours=1)
CLEANUP_INTERVAL = timedelta(minutes=30)

def init_memory():
    """Initialize LangGraph memory with synchronous database connection"""
    try:
        conn = psycopg.Connection.connect(
            DB_URI, autocommit=True, prepare_threshold=0, row_factory=dict_row
        )
        memory = PostgresSaver(conn)
        try:
            memory.setup()
            logger.info("LangGraph memory tables ensured (setup() completed)")
        except Exception as ex:
            logger.error(f"Failed to run PostgresSaver.setup(): {ex}")
            raise
        logger.info("LangGraph memory initialized successfully")
        return memory
    except Exception as e:
        logger.error(f"Failed to initialize memory: {e}")
        raise

@tool
async def save_user_info(name: str, location: str, session_id: str) -> str:
    """
    Save user's name and location to the database.
    """
    logger.info(f"save_user_info tool invoked: name={name}, location={location}, session_id={session_id}")
    try:
        if not connection_pool:
            logger.warning(f"Connection pool not available when calling save_user_info")
            return "Error: Database connection not available."

        async with connection_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO user_profiles (session_id, name, location, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (session_id)
                    DO UPDATE SET name = EXCLUDED.name, location = EXCLUDED.location, updated_at = CURRENT_TIMESTAMP;
                """, (session_id, name, location))

        if session_id in sessions:
            sessions[session_id]['user_info'] = {
                'name': name,
                'location': location,
                'info_captured_at': datetime.now().isoformat()
            }
        
        msg = f"Successfully saved information for {name} from {location}"
        logger.info(f"save_user_info completed successfully: {msg}")
        return msg
    except Exception as e:
        logger.error(f"DB Error in save_user_info: {e}", exc_info=True)
        return f"Error: Failed to save user information to database. ({str(e)})"


def get_station_info(city_name: str) -> tuple:
    """Fetch station information with caching"""
    try:
        api_url = f"https://api.railyatri.in/api/common_city_station_search.json?q={city_name}&hide_city=true&user_id=-1754124811"
        response = requests.get(api_url)
        response.raise_for_status()
        data = response.json()

        if data.get("success") and data.get("items"):
            first_station = data["items"][0]
            station_info = (
                first_station.get("station_name"),
                first_station.get("station_code")
            )
            return station_info

        return None, None

    except Exception as e:
        logger.error(f"Error fetching station info for {city_name}: {e}")
        return None, None

def correct_city_names(city_name: str) -> str:
    """
    Correct city name spellings with caching.
    """
    global model
    try:
        model = model

        prompt = f"""
        You are a geographical name correction expert. Your task is to correct and standardize city names.
        Input city: "{city_name}"
        Instructions:
        1. Correct any spelling mistakes in the city name
        2. Return only the corrected city name in proper case (first letter capitalized)
        3. If it's a well-known city with multiple names, use the most common official name
        4. If the input is unclear or not a valid city, return the most likely correct version
        5. Return ONLY the corrected city name, no explanations
        Corrected city name:
        """

        response = model.invoke(prompt)
        corrected_city = response.content.strip()
        corrected_city = re.sub(r'["\']', '', corrected_city)
        corrected_city = corrected_city.split('\n')[0].strip()

        logger.info(f"City name corrected: '{city_name}' → '{corrected_city}'")
        return corrected_city

    except Exception as e:
        logger.error(f"Error in city name correction for '{city_name}': {e}")
        fallback = city_name.title()
        return fallback

@tool
def search_trains(source: str, destination: str, date: str) -> str:
    """
    Search for trains between two locations on a given date.
    """
    try:
        source = correct_city_names(source)
        destination = correct_city_names(destination)
        source_info = get_station_info(source)
        dest_info = get_station_info(destination)
        source_code = source_info[1] if source_info else None
        destination_code = dest_info[1] if dest_info else None
    except Exception as e:
        logger.error(f"Error fetching station info: {e}")
        return json.dumps({"error": "Error fetching station details"})

    if not source_code or not destination_code:
        logger.error(f"Could not find station codes for {source} or {destination}")
        return json.dumps({"error": "Invalid source or destination"})

    try:
        date_obj = parser.parse(date)
        formatted = date_obj.strftime("%Y%m%d")
    except Exception as e:
        logger.error(f"Error parsing date {date}: {e}")
        return json.dumps({"error": "Invalid date format"})

    logger.info(f"Searching trains from {source} to {destination} on {formatted}")

    url = "https://www.redbus.in/rails/api/searchResults"
    payload = {
        "src": source_code,
        "dst": destination_code,
        "doj": formatted,
        "fcOpted": False
    }
    headers = {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://www.redbus.in",
        "referer": "https://www.redbus.in",
    }
    scraped_data = []

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        trains = data.get("trainBtwnStnsList", [])
        for train in trains:
            availability_info = train.get("tbsAvailability", [])
            fares = {}
            for available_class in availability_info:
                class_name = available_class.get("className")
                total_fare = available_class.get("totalFare")
                if class_name:
                    fares[class_name] = {
                        "fare": total_fare,
                        "status": available_class.get("availablityStatus"),
                        "seats_available": available_class.get("availablityNumber")
                    }
            train_data = {
                "train_name": train.get("trainName"),
                "train_number": train.get("trainNumber"),
                "departure_time": train.get("departureTime"),
                "arrival_time": train.get("arrivalTime"),
                "duration": train.get("duration"),
                "fares_and_availability": fares,
            }
            scraped_data.append(train_data)
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP request error searching trains: {e}")
        return json.dumps({"error": "HTTP request failed"})
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from train search response")
        return json.dumps({"error": "Invalid JSON response from provider"})

    return encode(scraped_data)

def get_current_datetime_info() -> Dict[str, Any]:
    """Get comprehensive current date and time information"""
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time": now.strftime("%H:%M:%S"),
        "current_day": now.strftime("%A"),
    }

@tool
def get_current_time():
    """Get the current IST time"""
    return get_current_datetime_info()

@tool
def fetch_bus_data(source: str, destination: str, date: str):
    """
    Fetch bus information from the local FastAPI RedBus server.
    """
    endpoint = f"http://127.0.0.1:8003/bus_search"
    source = correct_city_names(source)
    destination = correct_city_names(destination)
    params = {
        "source": source,
        "destination": destination,
        "date": date
    }
    try:
        response = requests.get(endpoint, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        result = data.get("results", [])
        return encode(result)
    except requests.exceptions.RequestException as e:
        return []

@tool
def web_search(query: str):
    """Search the web for travel or location-related information with caching."""
    logger.info(f"Executing web search with query: '{query}'")
    searcher = SearxSearchWrapper(searx_host="http://192.168.1.171:8080")
    try:
        results = searcher.run(query)
        return results
    except Exception as e:
        logger.error(f"Web search failed for query '{query}': {e}")
        return f"Error during web search: {e}"

@tool
def search_flights(from_city: str, to_city: str, date: str):
    """
    Search for flights between two cities.
    """
    try:
        date_obj = parser.parse(date)
        formatted_date = date_obj.strftime("%d/%m/%Y")
    except Exception as e:
        logger.error(f"Error parsing date {date}: {e}")
        return {"flights": []}
    from_city = correct_city_names(from_city)
    to_city = correct_city_names(to_city)
    payload = {
        "from_city": from_city,
        "to_city": to_city,
        "date": formatted_date
    }
    try:
        response = requests.post("http://192.168.1.171:8001/search-flights", json=payload)
        response.raise_for_status()
        result = response.json()
        return encode(result)
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error searching flights: {e}")
        return {"flights": []}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error searching flights: {e}")
        return {"flights": []}

tools = [save_user_info, search_trains, get_current_time, fetch_bus_data, web_search, search_flights]

system_prompt = """
# ROLE & IDENTITY
You are **Ravina**, a friendly and experienced travel agent at **JS Tech Alliance**. You're warm, professional, and passionate about helping customers plan their perfect trips.

Session ID: {session_id}

---

# USER CONTEXT
{user_context}

---

# CORE BEHAVIOR RULES

## 1. MANDATORY: User Information Collection (HIGHEST PRIORITY)
- **BEFORE doing anything else**, check if you have the customer's **name** AND **location/city**.
- If EITHER is missing, politely ask for both in a natural, conversational way.
- **IMMEDIATELY** call `save_user_info` tool once you have BOTH pieces of information.
- Do NOT proceed with any travel searches until user info is saved.

## 2. Travel Assistance Guidelines
Once user info is collected, help with:
- **Flights**: Use `search_flights` for air travel queries
- **Trains**: Use `search_trains` for rail travel queries  
- **Buses**: Use `fetch_bus_data` for bus travel queries
- **General Info**: Use `web_search` for destinations, hotels, tourist spots, etc.
- **Time Queries**: Use `get_current_time` when needed

## 3. Conversation Style
- Be warm, helpful, and conversational (not robotic)
- Use the customer's name naturally after learning it
- Provide clear, organized information (use bullet points for options)
- Proactively offer relevant suggestions and alternatives
- Ask clarifying questions when travel details are ambiguous (dates, preferences, budget)

## 4. Session Ending
When the customer says goodbye, thanks you, or indicates they're done:
- Thank them warmly for choosing JS Tech Alliance
- Wish them a great trip/day
- **MUST** end your response with `(disconnect)`

---

# RESPONSE FLOW
Greet → Check for name/location → Ask if missing
Receive name & location → CALL save_user_info IMMEDIATELY
Understand travel needs → Ask clarifying questions if needed
Search & present options → Offer alternatives
Handle follow-ups → End politely with (disconnect) when done
"""

def create_dynamic_agent(user_context: str, session_id: str):
    return create_agent(
        model=model,
        system_prompt=system_prompt.format(user_context=user_context, session_id=session_id),
        tools=tools,
        middleware=[
            ToolRetryMiddleware(max_retries=2),
            SummarizationMiddleware(model=model, max_tokens_before_summary=2500)
        ],
        checkpointer=memory
    )

class StartSessionRequest(BaseModel):
    message: str
    cid: Optional[str] = None

class SessionResponse(BaseModel):
    session_id: str
    created_at: str
    user_info: Optional[Dict[str, str]] = None

class SessionStatusResponse(BaseModel):
    session_id: str
    status: str
    created_at: str

class SessionResultResponse(BaseModel):
    session_id: str
    status: str
    response: Optional[str] = None
    error: Optional[str] = None
    user_info: Optional[Dict[str, str]] = None

class SessionHistoryResponse(BaseModel):
    session_id: str
    messages: List[Dict[str, str]]
    created_at: str
    user_info: Optional[Dict[str, str]] = None

async def cleanup_task():
    """Periodically clean up old job statuses and sessions."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL.total_seconds())
        logger.info("Running scheduled cleanup task...")
        
        # Cleanup job_status table
        async with connection_pool.connection() as conn:
            async with conn.cursor() as cur:
                cutoff_time = datetime.now(timezone.utc) - SESSION_TIMEOUT
                await cur.execute("DELETE FROM job_status WHERE created_at < %s", (cutoff_time,))
                logger.info(f"Cleaned up {cur.rowcount} old jobs from the database.")
        
        # Cleanup in-memory sessions
        expired_sids = [
            sid for sid, data in sessions.items()
            if datetime.now() - data['created_at'] > SESSION_TIMEOUT
        ]
        for sid in expired_sids:
            if sid in sessions:
                del sessions[sid]
                logger.info(f"Cleaned up expired in-memory session: {sid}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global memory, connection_pool
    logger.info("Travel Agent API is starting up...")
    await init_database()
    memory = init_memory()
    
    # Start the cleanup task in the background
    cleanup_future = asyncio.create_task(cleanup_task())
    
    logger.info(f"Agent initialized with {len(tools)} tools")
    yield
    
    # Shutdown
    cleanup_future.cancel()
    if connection_pool:
        await connection_pool.close()
    if memory and hasattr(memory, "conn"):
        try:
            memory.conn.close()
            logger.info("LangGraph memory connection closed")
        except Exception as e:
            logger.warning(f"Failed to close LangGraph memory connection: {e}")
    logger.info("Travel Agent API is shutting down...")

app = FastAPI(
    title="Travel Agent API",
    version="1.0.0",
    description="API for Ravina - AI Travel Agent from JS Tech Alliance",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def process_ai_task(session_id: str, message: str, cid: Optional[str] = None):
    """Background task to process AI logic."""
    try:
        logger.info(f"Starting AI processing for session {session_id}")
        thread_id = cid if cid else session_id
        
        user_info = await get_user_profile(thread_id)
        user_context = ""
        if user_info:
            user_context = f"Customer Name: {user_info['name']}, Customer Location: {user_info['location']}"
        else:
            user_context = "Customer information not yet collected. Please ask for their name and location."

        agent = create_dynamic_agent(user_context, thread_id)
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            agent.invoke,
            {'messages': [{'role': 'user', 'content': message}]},
            {"configurable": {"thread_id": thread_id}}
        )
        
        result = response['messages'][-1].content

        async with connection_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE job_status
                    SET status = %s, ai_response = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = %s
                """, ('complete', result, session_id))
        logger.info(f"AI processing for session {session_id} completed successfully.")

    except Exception as e:
        logger.error(f"Error during AI processing for session {session_id}: {e}", exc_info=True)
        async with connection_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE job_status
                    SET status = %s, error = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = %s
                """, ('failed', str(e), session_id))

@app.get("/")
async def root():
    return {
        "service": "Travel Agent API",
        "version": "1.0.0",
        "status": "running"
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "active_sessions": len(sessions),
    }

@app.post("/session/start", response_model=SessionResponse)
async def start_session(request: StartSessionRequest, background_tasks: BackgroundTasks):
    """Starts a new session and triggers background AI processing."""
    session_id = str(uuid4())
    
    async with connection_pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO job_status (session_id, cid, status, created_at)
                VALUES (%s, %s, %s, %s)
            """, (session_id, request.cid, 'processing', datetime.now(timezone.utc)))

    sessions[session_id] = {
        'created_at': datetime.now(),
        'user_info': None,
    }
    
    background_tasks.add_task(process_ai_task, session_id, request.message, request.cid)
    
    logger.info(f"Started new session {session_id} for CID {request.cid}")
    
    return SessionResponse(
        session_id=session_id,
        created_at=sessions[session_id]['created_at'].isoformat(),
        user_info=sessions[session_id]['user_info']
    )

@app.get("/session/status/{session_id}", response_model=SessionStatusResponse)
async def get_session_status(session_id: str):
    """Gets the status of a session."""
    async with connection_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT status, created_at FROM job_status WHERE session_id = %s", (session_id,))
            job = await cur.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Session not found")
            return SessionStatusResponse(
                session_id=session_id,
                status=job['status'],
                created_at=job['created_at'].isoformat()
            )

@app.get("/session/result/{session_id}", response_model=SessionResultResponse)
async def get_session_result(session_id: str):
    """Gets the result of a completed session."""
    async with connection_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT status, ai_response, error, cid FROM job_status WHERE session_id = %s", (session_id,))
            job = await cur.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Session not found")
            
            if job['status'] == 'processing':
                raise HTTPException(status_code=202, detail="Session is still processing")

            user_info = await get_user_profile(job['cid'])
            
            return SessionResultResponse(
                session_id=session_id,
                status=job['status'],
                response=job.get('ai_response'),
                error=job.get('error'),
                user_info=user_info
            )

@app.get("/session/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(session_id: str):
    """Get conversation history for a session from the checkpointer"""
    if not memory:
        raise HTTPException(status_code=500, detail="Memory system not initialized")

    async with connection_pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT cid FROM job_status WHERE session_id = %s", (session_id,))
            job = await cur.fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Session not found")
            
            thread_id = job['cid']

    config = {"configurable": {"thread_id": thread_id}}
    thread_state = memory.get(config)

    if not thread_state:
        messages_from_memory = []
    else:
        if hasattr(thread_state, 'values'):
            channel_values = getattr(thread_state, 'values')
        elif isinstance(thread_state, dict):
            channel_values = thread_state.get('channel_values', {})
        else:
            channel_values = {}
        messages_from_memory = channel_values.get('messages', []) if isinstance(channel_values, dict) else []

    formatted_messages = []
    for msg in messages_from_memory:
        if isinstance(msg, HumanMessage):
            role = 'user'
        elif isinstance(msg, AIMessage):
            role = 'assistant'
        else:
            continue
        formatted_messages.append({'role': role, 'content': msg.content})

    return SessionHistoryResponse(
        session_id=session_id,
        messages=formatted_messages,
        created_at=datetime.now().isoformat(),
        user_info= await get_user_profile(thread_id)
    )

if __name__ == "__main__":
    logger.info("Starting Travel Agent API server...")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8004,
        log_level="info"
    )
