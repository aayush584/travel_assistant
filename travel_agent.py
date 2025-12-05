# from langchain.agents import create_agent
# from langchain_ollama import ChatOllama
# import requests, json
# import logging
# from dateutil import parser
# from datetime import datetime, timedelta
# from typing import Dict, Any, List, Optional
# from langchain.agents.middleware import ToolRetryMiddleware, SummarizationMiddleware
# from langchain_community.utilities import SearxSearchWrapper
# from fastapi import FastAPI, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel
# from uuid import uuid4
# import uvicorn
# from langchain.tools import tool
# from contextlib import asynccontextmanager
# from toon_format import encode
# import psycopg, re
# from psycopg.rows import dict_row
# from psycopg_pool import AsyncConnectionPool  
# # --- Imports for LangGraph Memory ---
# from langgraph.checkpoint.postgres import PostgresSaver
# from langchain_core.messages import HumanMessage, AIMessage 

# # Configure logging
# logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO)

# # Database configuration
# DB_URI = "postgresql://developer:password@localhost:5432/users"

# # Initialize the chat model
# model = ChatOllama(model="gpt-oss:20b-cloud", temperature=0)

# # Connection pool for async operations
# connection_pool = None
# # Memory will be initialized in lifespan
# memory = None

# async def init_database():
#     """Initialize database connection pool and create tables"""
#     global connection_pool
#     try:
#         connection_pool = AsyncConnectionPool(
#             conninfo=DB_URI,
#             min_size=1,
#             max_size=10,
#             open=False
#         )
#         await connection_pool.open()
#         await ensure_tables_exist()
#         logger.info("Database connection pool initialized successfully")
#         return connection_pool
#     except Exception as e:
#         logger.error(f"Failed to initialize database: {e}")
#         raise

# async def ensure_tables_exist():
#     """Creates the necessary tables for storing user info if they don't exist."""
#     try:
#         async with connection_pool.connection() as conn:
#             async with conn.cursor() as cur:
#                 # User profiles table
#                 await cur.execute("""
#                     CREATE TABLE IF NOT EXISTS user_profiles (
#                         session_id TEXT PRIMARY KEY,
#                         name TEXT,
#                         location TEXT,
#                         updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#                     );
#                 """)
#         logger.info("Database tables verified.")
#     except Exception as e:
#         logger.error(f"Error ensuring tables exist: {e}")
#         raise

# async def get_user_profile(session_id: str):
#     """Fetch user info from the database by session_id."""
#     if not connection_pool:
#         return None
    
#     try:
#         async with connection_pool.connection() as conn:
#             async with conn.cursor() as cur:
#                 await cur.execute(
#                     "SELECT name, location FROM user_profiles WHERE session_id = %s", 
#                     (session_id,)
#                 )
#                 row = await cur.fetchone()
#                 if row:
#                     return {"name": row[0], "location": row[1]}
#                 return None
#     except Exception as e:
#         logger.error(f"Error fetching user profile: {e}")
#         return None

# def _load_user_info_into_session(session_id: str):
#     """
#     Load user info from database (if it exists) and populate the session.
#     Call this when resuming or creating a session to give the agent context.
#     """
#     # Try to load from database synchronously
#     try:
#         import asyncio
        
#         async def _fetch():
#             return await get_user_profile(session_id)
        
#         try:
#             loop = asyncio.get_event_loop()
#             if loop.is_running():
#                 import concurrent.futures
#                 with concurrent.futures.ThreadPoolExecutor() as pool:
#                     user_info = pool.submit(asyncio.run, _fetch()).result()
#             else:
#                 user_info = asyncio.run(_fetch())
#         except RuntimeError:
#             user_info = asyncio.run(_fetch())
        
#         if user_info and session_id in sessions:
#             sessions[session_id]['user_info'] = {
#                 'name': user_info['name'],
#                 'location': user_info['location'],
#                 'loaded_from_db': "True",
#                 'loaded_at': datetime.now().isoformat()
#             }
#             logger.info(f"Loaded user info from database for session {session_id}: {user_info['name']} from {user_info['location']}")
#             return True
#         return False
#     except Exception as e:
#         logger.warning(f"Failed to load user info from database for session {session_id}: {e}")
#         return False

# # Session storage for metadata only
# sessions: Dict[str, Dict] = {}
# SESSION_TIMEOUT = timedelta(hours=1)

# def init_memory():
#     """Initialize LangGraph memory with synchronous database connection"""
#     try:
#         # Create a persistent psycopg Connection and instantiate PostgresSaver
#         # PostgresSaver.from_conn_string is a contextmanager that yields a
#         # PostgresSaver; calling it directly returns a generator-based
#         # context manager object which is why we were seeing
#         # '_GeneratorContextManager' earlier. Create the Connection directly
#         # and pass it to PostgresSaver so the returned object is the
#         # expected saver instance.
#         conn = psycopg.Connection.connect(
#             DB_URI, autocommit=True, prepare_threshold=0, row_factory=dict_row
#         )
#         memory = PostgresSaver(conn)
#         # Ensure DB schema for LangGraph checkpoints exists.
#         # PostgresSaver.setup() runs migrations and creates the `checkpoints`,
#         # `checkpoint_blobs`, and `checkpoint_writes` tables if needed. Calling
#         # setup() here ensures the tables exist before any agent invocation.
#         try:
#             memory.setup()
#             logger.info("LangGraph memory tables ensured (setup() completed)")
#         except Exception as ex:
#             # If setup fails, surface the error so the app d
#             # oesn't start in a
#             # partially-initialized state.
#             logger.error(f"Failed to run PostgresSaver.setup(): {ex}")
#             raise
#         logger.info("LangGraph memory initialized successfully")
#         return memory
#     except Exception as e:
#         logger.error(f"Failed to initialize memory: {e}")
#         raise

# # Add this new tool to save user information
# @tool
# def save_user_info(name: str, location: str, session_id: str) -> str:
#     """
#     Save user's name and location to the database.
    
#     Args:
#         name: User's name
#         location: User's current location
#         session_id: Current session ID
        
#     Returns:
#         Confirmation message
#     """
#     logger.info(f"save_user_info tool invoked: name={name}, location={location}, session_id={session_id}")
#     try:
#         if not connection_pool:
#             logger.warning(f"Connection pool not available when calling save_user_info")
#             return "Error: Database connection not available."

#         # Use sync approach: run async code via asyncio
#         import asyncio
        
#         async def _insert_user_info():
#             async with connection_pool.connection() as conn:
#                 async with conn.cursor() as cur:
#                     # Upsert (Insert or Update if exists)
#                     logger.debug(f"Executing upsert for session_id={session_id}, name={name}, location={location}")
#                     await cur.execute("""
#                         INSERT INTO user_profiles (session_id, name, location, updated_at)
#                         VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
#                         ON CONFLICT (session_id) 
#                         DO UPDATE SET name = EXCLUDED.name, location = EXCLUDED.location, updated_at = CURRENT_TIMESTAMP;
#                     """, (session_id, name, location))
#                     logger.debug(f"Upsert completed for session_id={session_id}")
        
#         # Run the async insert
#         try:
#             loop = asyncio.get_event_loop()
#             if loop.is_running():
#                 # If we're already in an async context, use a new event loop in a thread
#                 import concurrent.futures
#                 with concurrent.futures.ThreadPoolExecutor() as pool:
#                     future = pool.submit(asyncio.run, _insert_user_info())
#                     future.result()
#             else:
#                 asyncio.run(_insert_user_info())
#         except RuntimeError:
#             asyncio.run(_insert_user_info())
            
#         # Also update in-memory session
#         if session_id in sessions:
#             sessions[session_id]['user_info'] = {
#                 'name': name,
#                 'location': location,
#                 'info_captured_at': datetime.now().isoformat()
#             }
#             logger.info(f"Updated in-memory session {session_id} with user info")
            
#         msg = f"Successfully saved information for {name} from {location}"
#         logger.info(f"save_user_info completed successfully: {msg}")
#         return msg
#     except Exception as e:
#         logger.error(f"DB Error in save_user_info: {e}", exc_info=True)
#         return f"Error: Failed to save user information to database. ({str(e)})"

# def get_station_info(city_name: str) -> tuple:
#     """Fetch station information with caching"""
#     try:
#         api_url = f"https://api.railyatri.in/api/common_city_station_search.json?q={city_name}&hide_city=true&user_id=-1754124811"
#         response = requests.get(api_url)
#         response.raise_for_status()
#         data = response.json()
        
#         if data.get("success") and data.get("items"):
#             first_station = data["items"][0]
#             station_info = (
#                 first_station.get("station_name"),
#                 first_station.get("station_code")
#             )
#             return station_info
        
#         return None, None
        
#     except Exception as e:
#         logger.error(f"Error fetching station info for {city_name}: {e}")
#         return None, None

# def correct_city_names(city_name: str) -> str:
#     """
#     Correct city name spellings with caching.
#     Args:
#         city_name: Input city name that might have spelling errors
#     Returns:
#         Corrected city name
#     """
#     # Check cache first
#     global model
#     try:
#         model = model
        
#         prompt = f"""
#         You are a geographical name correction expert. Your task is to correct and standardize city names.
        
#         Input city: "{city_name}"
        
#         Instructions:
#         1. Correct any spelling mistakes in the city name
#         2. Return only the corrected city name in proper case (first letter capitalized)
#         3. If it's a well-known city with multiple names, use the most common official name
#         4. If the input is unclear or not a valid city, return the most likely correct version
#         5. Return ONLY the corrected city name, no explanations
        
#         Examples:
#         - "mumbay" → "Mumbai"
#         - "new delhi" → "New Delhi" 
#         - "bangalore" → "Bengaluru"
#         - "calcutta" → "Kolkata"
#         - "madras" → "Chennai"
#         - "puna" → "Pune"
#         - "hydrabad" → "Hyderabad"
#         - "jaipur" → "Jaipur"
#         - "delhi" → "New Delhi"
        
#         Corrected city name:
#         """
        
#         response = model.invoke(prompt)
#         corrected_city = response.content.strip()
        
#         # Clean up the response
#         corrected_city = re.sub(r'["\']', '', corrected_city)
#         corrected_city = corrected_city.split('\n')[0].strip()
        
#         # Cache the result
        
#         logger.info(f"City name corrected: '{city_name}' → '{corrected_city}'")
#         return corrected_city
        
#     except Exception as e:
#         logger.error(f"Error in city name correction for '{city_name}': {e}")
#         # Fallback: return original name with basic capitalization
#         fallback = city_name.title()

#         return fallback


# @tool
# def search_trains(source: str, destination: str, date: str) -> str:
#     """
#     Args:
#         source (str): Source location
#         destination (str): Destination location
#         date (str): Date of travel in YYYY-MM-DD format

#     Returns:
#         str: JSON string containing list of train details
#     """
    
#     # 1. Validate Station Codes
#     try:
#         source = correct_city_names(source)
#         destination = correct_city_names(destination)
#         source_info = get_station_info(source)
#         dest_info = get_station_info(destination)
#         source_code = source_info[1] if source_info else None
#         destination_code = dest_info[1] if dest_info else None
#     except Exception as e:
#         logger.error(f"Error fetching station info: {e}")
#         return json.dumps({"error": "Error fetching station details"})

#     if not source_code or not destination_code:
#         logger.error(f"Could not find station codes for {source} or {destination}")
#         return json.dumps({"error": "Invalid source or destination"})

#     # 2. Validate Date
#     try:
#         date_obj = parser.parse(date)
#         formatted = date_obj.strftime("%Y%m%d")
#     except Exception as e:
#         logger.error(f"Error parsing date {date}: {e}")
#         return json.dumps({"error": "Invalid date format"})

#     logger.info(f"Searching trains from {source} to {destination} on {formatted}")
    
#     url = "https://www.redbus.in/rails/api/searchResults"
#     payload = {
#         "src": source_code,
#         "dst": destination_code,
#         "doj": formatted,
#         "fcOpted": False
#     }

#     headers = {
#         "accept": "*/*",
#         "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
#         "content-type": "application/json",
#         "origin": "https://www.redbus.in",
#         "referer": "https://www.redbus.in",
#     }

#     scraped_data = []

#     try:
#         response = requests.post(url, json=payload, headers=headers)
#         response.raise_for_status()

#         data = response.json()
#         trains = data.get("trainBtwnStnsList", [])

#         for train in trains:
#             availability_info = train.get("tbsAvailability", [])
#             fares = {}

#             # Process fares
#             for available_class in availability_info:
#                 class_name = available_class.get("className")
#                 total_fare = available_class.get("totalFare")

#                 if class_name:
#                     fares[class_name] = {
#                         "fare": total_fare,
#                         "status": available_class.get("availablityStatus"),
#                         "seats_available": available_class.get("availablityNumber")
#                     }

#             # Build dictionary
#             train_data = {
#                 "train_name": train.get("trainName"),
#                 "train_number": train.get("trainNumber"),
#                 "departure_station_code": train.get("fromStnCode"),
#                 "departure_station_name": train.get("fromStnName"),
#                 "arrival_station_code": train.get("toStnCode"),
#                 "arrival_station_name": train.get("toStnName"),
#                 "departure_time": train.get("departureTime"),
#                 "departure_date": train.get("departureDate"),
#                 "arrival_time": train.get("arrivalTime"),
#                 "arrival_date": train.get("arrivalDate"),
#                 "duration": train.get("duration"),
#                 "distance": train.get("distance"),
#                 "available_classes": train.get("avlClasses", []),
#                 "fares_and_availability": fares,
#                 # Flat structure for running days
#                 "run_sun": train.get("runningSun"),
#                 "run_mon": train.get("runningMon"),
#                 "run_tue": train.get("runningTue"),
#                 "run_wed": train.get("runningWed"),
#                 "run_thu": train.get("runningThu"),
#                 "run_fri": train.get("runningFri"),
#                 "run_sat": train.get("runningSat"),
#             }
            
#             scraped_data.append(train_data)

#     except requests.exceptions.RequestException as e:
#         logger.error(f"HTTP request error searching trains: {e}")
#         return json.dumps({"error": "HTTP request failed"})
#     except json.JSONDecodeError:
#         logger.error("Failed to decode JSON from train search response")
#         return json.dumps({"error": "Invalid JSON response from provider"})

#     # Return standard flat JSON string
#     return encode(scraped_data)

# def get_current_datetime_info() -> Dict[str, Any]:
#     """Get comprehensive current date and time information"""
#     now = datetime.utcnow() + timedelta(hours=5, minutes=30)
#     return {
#         "current_date": now.strftime("%Y-%m-%d"),
#         "current_time": now.strftime("%H:%M:%S"),
#         "current_day": now.strftime("%A"),
#         "current_datetime_full": now.strftime("%Y-%m-%d %H:%M:%S"),
#         "timezone": "IST (UTC+5:30)"
#     }

# @tool
# def get_current_time():
#     """Get the current IST time"""
#     return get_current_datetime_info()
    
# @tool    
# def fetch_bus_data(source: str, destination: str, date: str):
#     """
#     Fetch bus information from the local FastAPI RedBus server.
    
#     Args:
#         source (str): Source city name
#         destination (str): Destination city name
#         date (str): Journey date in YYYY-MM-DD format
        
#     Returns:
#         list: A list of bus data dictionaries
#     """
#     endpoint = f"http://127.0.0.1:8003/bus_search"
#     source = correct_city_names(source)
#     destination = correct_city_names(destination)
#     params = {
#         "source": source,
#         "destination": destination,
#         "date": date
#     }
    
#     try:
#         response = requests.get(endpoint, params=params, timeout=15)
#         response.raise_for_status()
#         data = response.json()
#         print(f"Successfully fetched {len(data.get('results', []))} buses from API.")
#         result = data.get("results", [])
#         return encode(result)

#     except requests.exceptions.RequestException as e:
#         print(f"❌ API request failed: {e}")
#         return []

# @tool
# def web_search(query: str):
#     """Search the web for travel or location-related information with caching."""
#     logger.info(f"Executing web search with query: '{query}'")
#     searcher = SearxSearchWrapper(searx_host="http://192.168.1.171:8080")
#     try:
#         results = searcher.run(query)
#         logger.info(f"Web search returned {len(results)} characters")
#         return results
#     except Exception as e:
#         logger.error(f"Web search failed for query '{query}': {e}")
#         return f"Error during web search: {e}"

# @tool
# def search_flights(from_city: str, to_city: str, date: str):
#     """
#     Search for flights between two cities.
#     Args:
#         from_city: Departure city name.
#         to_city: Arrival city name.
#         date: Travel date in YYYY-MM-DD format.
#     Returns:
#         JSON string of flight options.
#     """
#     try:
#         date_obj = parser.parse(date)
#         formatted_date = date_obj.strftime("%d/%m/%Y")
#     except Exception as e:
#         logger.error(f"Error parsing date {date}: {e}")
#         return {"flights": []}
#     from_city = correct_city_names(from_city)
#     to_city = correct_city_names(to_city)
#     payload = {
#         "from_city": from_city,
#         "to_city": to_city,
#         "date": formatted_date
#     }

#     try:
#         response = requests.post("http://192.168.1.171:8001/search-flights", json=payload)
#         response.raise_for_status()
#         result = response.json()
#         return encode(result)

#     except requests.exceptions.HTTPError as e:
#         logger.error(f"HTTP error searching flights: {e}")
#         return {"flights": []}
#     except requests.exceptions.RequestException as e:
#         logger.error(f"Network error searching flights: {e}")
#         return {"flights": []}

# # Define tools list - include save_user_info tool
# tools = [save_user_info, search_trains, get_current_time, fetch_bus_data, web_search, search_flights]

# # Modified system prompt to collect user info
# system_prompt = """
# You are Ravina from JS Tech Alliance, a friendly and experienced travel agent helping customers to give travel advice over the phone.
# Speak naturally as if you're sitting across from the customer—warm, helpful, and real.

# **IMPORTANT: SAVING USER INFO**
# Your session_id is: {session_id}
# Use the save_user_info tool to persist the customer's name and location to the database. This is MANDATORY.
# When the customer provides their name and location (or you extract it from conversation), IMMEDIATELY call save_user_info with:
#   - name: (customer's name)
#   - location: (customer's location)
#   - session_id: {session_id}

# Only after saving user info can the conversation proceed smoothly. If you don't save it, the system won't remember the customer's details.

# CRITICAL FIRST STEP:
# - If you haven't already collected the customer's name and location, politely ask for them first.
# - As soon as you have BOTH name and location, call the save_user_info tool immediately.
# - After calling save_user_info (and it returns successfully), address the customer by their name throughout the conversation.
# - Consider their location when providing travel suggestions.

# PRIMARY GOALS:
# - Always personalize the conversation using the customer's name once you know it.
# - Check current time first when providing time-sensitive information.
# - Understand the customer's travel needs and preferences.
# - Provide accurate and helpful travel information using the available tools.
# - Always end with a conversational question.
# - Sound like a real person on the phone, not a chatbot.
# - Use the tools to gather information when needed; do not hesitate to use save_user_info when you have name and location.
# - Do not use table formats or lists in your responses.
# - When providing options (like train or flight choices), present them conversationally rather than as a list.
# - Keep your response short, concise and to the point.

# USER CONTEXT:
# {user_context}
# """

# # Create the agent with dynamic system prompt
# def create_dynamic_agent(user_context: str, session_id: str):
#     return create_agent(
#         model=model,
#         system_prompt=system_prompt.format(user_context=user_context, session_id=session_id),
#         tools=tools,
#         middleware=[
#             ToolRetryMiddleware(max_retries=2),
#             SummarizationMiddleware(model=model, max_tokens_before_summary=2500)
#         ],
#         checkpointer=memory
#     )

# # Pydantic models for request/response
# class ChatMessage(BaseModel):
#     role: str
#     content: str

# class ChatRequest(BaseModel):
#     message: str
#     cid: Optional[str] = None
#     session_id: Optional[str] = None

# class ChatResponse(BaseModel):
#     response: str
#     session_id: str
#     cid: Optional[str] = None
#     success: bool
#     error: Optional[str] = None
#     user_info: Optional[Dict[str, str]] = None

# class SessionResponse(BaseModel):
#     session_id: str
#     created_at: str
#     user_info: Optional[Dict[str, str]] = None

# class SessionHistoryResponse(BaseModel):
#     session_id: str
#     messages: List[Dict[str, str]]
#     created_at: str
#     user_info: Optional[Dict[str, str]] = None

# def cleanup_old_sessions():
#     """Remove expired sessions"""
#     current_time = datetime.now()
#     expired = [
#         sid for sid, data in sessions.items()
#         if current_time - data['created_at'] > SESSION_TIMEOUT
#     ]
#     for sid in expired:
#         del sessions[sid]
#         if memory and hasattr(memory, 'storage'):
#             config = {"configurable": {"thread_id": sid}}
#             memory.storage.delete(config)
#         logger.info(f"Cleaned up expired session: {sid}")

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # Startup
#     global memory, connection_pool
#     logger.info("Travel Agent API is starting up...")
    
#     # Initialize database connection pool for async operations
#     await init_database()
    
#     # Initialize LangGraph memory with synchronous connection
#     memory = init_memory()
    
#     logger.info(f"Agent initialized with {len(tools)} tools")
#     yield
    
#     # Shutdown
#     if connection_pool:
#         await connection_pool.close()
#     # If we created a persistent Postgres connection for LangGraph memory, close it
#     if memory and hasattr(memory, "conn"):
#         try:
#             memory.conn.close()
#             logger.info("LangGraph memory connection closed")
#         except Exception as e:
#             logger.warning(f"Failed to close LangGraph memory connection: {e}")
#     logger.info("Travel Agent API is shutting down...")

# # Create FastAPI app with lifespan
# app = FastAPI(
#     title="Travel Agent API", 
#     version="1.0.0",
#     description="API for Ravina - AI Travel Agent from JS Tech Alliance",
#     lifespan=lifespan
# )

# # Add CORS middleware
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# @app.get("/")
# async def root():
#     """Root endpoint"""
#     return {
#         "service": "Travel Agent API",
#         "version": "1.0.0",
#         "agent": "Ravina from JS Tech Alliance",
#         "status": "running"
#     }

# @app.get("/health")
# async def health_check():
#     """Health check endpoint"""
#     cleanup_old_sessions()
#     return {
#         "status": "healthy",
#         "service": "travel-agent",
#         "active_sessions": len(sessions),
#         "tools_available": len(tools)
#     }

# @app.post("/session/create", response_model=SessionResponse)
# async def create_session():
#     """Create a new conversation session"""
#     session_id = str(uuid4())
#     sessions[session_id] = {
#         'created_at': datetime.now(),
#         'user_info': None
#     }
#     cleanup_old_sessions()
    
#     # Try to load user info from database if it exists for this session
#     # (useful if client is re-creating a session for a known user)
#     _load_user_info_into_session(session_id)
    
#     logger.info(f"Created new session: {session_id}")
    
#     return SessionResponse(
#         session_id=session_id,
#         created_at=sessions[session_id]['created_at'].isoformat(),
#         user_info=sessions[session_id]['user_info']
#     )

# @app.post("/chat", response_model=ChatResponse)
# async def chat_endpoint(request: ChatRequest):
#     """
#     Main chat endpoint with session support
#     Maintains conversation history across multiple requests
#     """
#     try:
#         # Use cid as the primary session identifier if provided
#         session_id = request.cid if request.cid else request.session_id

#         # Create or retrieve session
#         if not session_id or session_id not in sessions:
#             session_id = request.cid or str(uuid4())
#             sessions[session_id] = {
#                 'created_at': datetime.now(),
#                 'user_info': None
#             }
#             logger.info(f"Created new session: {session_id}")
#         else:
#             logger.info(f"Using existing session: {session_id}")
#             # Load user info from database if not already in memory
#             if not sessions[session_id].get('user_info'):
#                 _load_user_info_into_session(session_id)
        
#         logger.info(f"Session {session_id}: User message - {request.message[:50]}...")
        
#         # Prepare user context for the agent
#         user_context = ""
#         if sessions[session_id].get('user_info'):
#             user_info = sessions[session_id]['user_info']
#             user_context = f"Customer Name: {user_info['name']}, Customer Location: {user_info['location']}"
#         else:
#             user_context = "Customer information not yet collected. Please ask for their name and location."
        
#         # Create agent with session-specific context
#         agent = create_dynamic_agent(user_context, session_id)
        
#         # Invoke agent with the checkpointer
#         response = agent.invoke(
#             {'messages': [{'role': 'user', 'content': request.message}]},
#             {"configurable": {"thread_id": session_id}}
#         )

#         # Extract the latest response from the agent
#         result = response['messages'][-1].content
        
#         logger.info(f"Session {session_id}: Agent response - {result}...")
        
#         # Update session timestamp to prevent premature cleanup
#         sessions[session_id]['created_at'] = datetime.now()
#         # Persist conversation into LangGraph checkpointer
#         if memory:
#             try:
#                 config = {"configurable": {"thread_id": session_id}}
#                 thread_state = memory.get(config)
#                 checkpoint = thread_state or {
#                     "v": 1, "ts": datetime.utcnow().isoformat()+"Z", "id": str(uuid4()),
#                     "channel_values": {}, "channel_versions": {}, "versions_seen": {}, "updated_channels": []
#                 }

#                 channel_values = checkpoint.get('channel_values', {}) or {}
#                 msgs = channel_values.get('messages', []) or []
#                 msgs.append(HumanMessage(content=request.message))
#                 msgs.append(AIMessage(content=result))
#                 channel_values['messages'] = msgs
#                 checkpoint['channel_values'] = channel_values

#                 memory.put(config, checkpoint, {"source": "update"}, {})
#                 logger.info(f"Session {session_id}: conversation persisted to memory (messages={len(msgs)})")
#             except Exception as e:
#                 logger.warning(f"Session {session_id}: error while persisting conversation to memory: {e}")

#         return ChatResponse(
#             response=result,
#             session_id=session_id,
#             cid=request.cid,
#             success=True,
#             user_info=sessions[session_id].get('user_info')
#         )
    
#     except Exception as e:
#         logger.error(f"Error processing chat request: {e}", exc_info=True)
#         raise HTTPException(
#             status_code=500, 
#             detail=f"Error processing request: {str(e)}"
#         )

# @app.get("/session/{session_id}/history", response_model=SessionHistoryResponse)
# async def get_session_history(session_id: str):
#     """Get conversation history for a session from the checkpointer"""
#     if session_id not in sessions:
#         raise HTTPException(status_code=404, detail="Session not found")
    
#     if not memory:
#         raise HTTPException(status_code=500, detail="Memory system not initialized")
    
#     # Fetch history from the LangGraph memory checkpointer
#     config = {"configurable": {"thread_id": session_id}}
#     thread_state = memory.get(config)

#     # Normalize channel values whether thread_state is a dict (Checkpoint) or
#     # an object with a `.values` attribute.
#     if not thread_state:
#         messages_from_memory = []
#     else:
#         if hasattr(thread_state, 'values'):
#             channel_values = getattr(thread_state, 'values')
#         elif isinstance(thread_state, dict):
#             channel_values = thread_state.get('channel_values', {})
#         else:
#             channel_values = {}

#         messages_from_memory = channel_values.get('messages', []) if isinstance(channel_values, dict) else []
    
#     # Convert message objects to the dictionary format expected by the API
#     formatted_messages = []
#     for msg in messages_from_memory:
#         if isinstance(msg, HumanMessage):
#             role = 'user'
#         elif isinstance(msg, AIMessage):
#             role = 'assistant'
#         else:
#             # Skip other message types like ToolMessage if you don't want them in the history
#             continue
#         formatted_messages.append({'role': role, 'content': msg.content})

#     return SessionHistoryResponse(
#         session_id=session_id,
#         messages=formatted_messages,
#         created_at=sessions[session_id]['created_at'].isoformat(),
#         user_info=sessions[session_id].get('user_info')
#     )

# @app.delete("/session/{session_id}")
# async def delete_session(session_id: str):
#     """Delete a conversation session"""
#     if session_id in sessions:
#         del sessions[session_id]
#         # Also delete the thread from the LangGraph memory
#         if memory and hasattr(memory, 'storage'):
#             config = {"configurable": {"thread_id": session_id}}
#             memory.storage.delete(config)
#         logger.info(f"Deleted session and memory for: {session_id}")
#         return {"message": "Session deleted successfully", "session_id": session_id}
#     raise HTTPException(status_code=404, detail="Session not found")

# @app.get("/sessions")
# async def list_sessions():
#     """List all active sessions"""
#     cleanup_old_sessions()
    
#     session_list = []
#     for sid, data in sessions.items():
#         message_count = 0
#         if memory:
#             config = {"configurable": {"thread_id": sid}}
#             thread_state = memory.get(config)
#             if not thread_state:
#                 message_count = 0
#             else:
#                 if hasattr(thread_state, 'values'):
#                     channel_values = getattr(thread_state, 'values')
#                 elif isinstance(thread_state, dict):
#                     channel_values = thread_state.get('channel_values', {})
#                 else:
#                     channel_values = {}
#                 message_count = len(channel_values.get('messages', [])) if isinstance(channel_values, dict) else 0
        
#         session_list.append({
#             "session_id": sid,
#             "message_count": message_count,
#             "created_at": data['created_at'].isoformat(),
#             "user_info": data.get('user_info')
#         })

#     return {
#         "total_sessions": len(sessions),
#         "sessions": session_list
#     }

# # New endpoint to manually update user info (optional)
# @app.put("/session/{session_id}/user-info")
# async def update_user_info(session_id: str, name: str, location: str):
#     """Manually update user information for a session"""
#     if session_id not in sessions:
#         raise HTTPException(status_code=404, detail="Session not found")
    
#     sessions[session_id]['user_info'] = {
#         'name': name,
#         'location': location,
#         'info_captured_at': datetime.now().isoformat()
#     }
    
#     # Also save to database
#     await save_user_info(name, location, session_id)
    
#     return {
#         "message": "User info updated successfully",
#         "session_id": session_id,
#         "user_info": sessions[session_id]['user_info']
#     }

# # ======================= Main Entry Point =======================

# if __name__ == "__main__":
#     # Run FastAPI server
#     logger.info("Starting Travel Agent API server...")
#     uvicorn.run(
#         app, 
#         host="0.0.0.0", 
#         port=8004,
#         log_level="info"
#     )


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
