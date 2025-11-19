from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
import uvicorn
import logging
from datetime import datetime, timedelta
from dateutil import parser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import re
from functools import lru_cache
from collections import OrderedDict
import threading
import asyncio
import uuid
# Import your existing components
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain.agents.middleware import SummarizationMiddleware, TodoListMiddleware, LLMToolSelectorMiddleware, ToolRetryMiddleware
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_community.utilities import SearxSearchWrapper
from langchain_core.tools import tool
from langchain.agents.structured_output import ToolStrategy
import time 
from contextlib import contextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= CONFIGURATION =================
class Config:
    # API Settings
    API_TITLE = "Travel Assistant API"
    API_VERSION = "1.0.0"
    HOST = "0.0.0.0"
    PORT = 8002
    
    # Model Settings
    MODEL_NAME = "gpt-oss:20b-cloud"
    MODEL_TEMPERATURE = 0
    CORRECTION_MODEL_TEMPERATURE = 0.1
    
    # Session Settings
    MAX_SESSION_AGE_HOURS = 24
    MAX_CHAT_HISTORY = 20
    SESSION_CLEANUP_INTERVAL = 3600  # 1 hour
    
    # Cache Settings
    CITY_CODE_CACHE_SIZE = 500
    STATION_INFO_CACHE_SIZE = 500
    CITY_CORRECTION_CACHE_SIZE = 1000
    WEB_SEARCH_CACHE_SIZE = 100
    CACHE_TTL = 3600  # 1 hour
    
    # Request Settings
    REQUEST_TIMEOUT = 10
    MAX_RETRIES = 3
    BACKOFF_FACTOR = 0.3
    
    # External APIs
    REDBUS_BASE_URL = "https://www.redbus.in"
    RAILYATRI_BASE_URL = "https://api.railyatri.in"
    FLIGHT_API_URL = "http://192.168.1.171:8001/search-flights/"
    SEARX_HOST = "http://192.168.1.171:8080"
    
    # Response Settings
    MAX_RESPONSE_WORDS = 20
    MAX_OPTIONS_TO_SHOW = 3

config = Config()

# ================= INITIALIZE APP =================
app = FastAPI(
    title=config.API_TITLE,
    description="A comprehensive travel assistant for train and flight searches",
    version=config.API_VERSION
)


class ToolExecutionTracker:
    def __init__(self):
        self.active_sessions = {}
        self.lock = threading.Lock()
    
    def start_tool_execution(self, session_id: str):
        """Mark that a tool execution has started for a session"""
        with self.lock:
            self.active_sessions[session_id] = {
                "tool_start_time": time.time(),
                "status": "executing_tool"
            }
            logger.info(f"Tool execution started for session {session_id}")
    
    def finish_tool_execution(self, session_id: str):
        """Mark that tool execution has finished for a session"""
        with self.lock:
            if session_id in self.active_sessions:
                execution_time = time.time() - self.active_sessions[session_id]["tool_start_time"]
                self.active_sessions[session_id]["status"] = "ready"
                self.active_sessions[session_id]["last_execution_time"] = execution_time
                logger.info(f"Tool execution finished for session {session_id} in {execution_time:.2f}s")
    
    def is_executing_tool(self, session_id: str) -> bool:
        """Check if a tool is currently executing for a session"""
        with self.lock:
            session_data = self.active_sessions.get(session_id, {})
            return session_data.get("status") == "executing_tool"
    
    def get_session_status(self, session_id: str) -> dict:
        """Get current status of a session"""
        with self.lock:
            return self.active_sessions.get(session_id, {"status": "unknown"})

# Initialize the tracker
tool_tracker = ToolExecutionTracker()

# Add context manager for tool execution
@contextmanager
def tool_execution_context(session_id: str):
    """Context manager to track tool execution with proper start/finish"""
    tool_tracker.start_tool_execution(session_id)
    try:
        yield
    finally:
        tool_tracker.finish_tool_execution(session_id)

# ================= MODELS =================
class FlightTrainInfo(BaseModel):
    source: str
    destination: str
    date: str

class ChatRequest(BaseModel):
    message: str = Field(..., description="User message to the travel assistant")
    session_id: Optional[str] = Field(None, description="Session ID for maintaining conversation history")
    user_name: Optional[str] = Field(None, description="User's name for personalization")
    user_location: Optional[str] = Field(None, description="User's location for context")

class ChatResponse(BaseModel):
    response: str = Field(..., description="Assistant's response")
    session_id: Optional[str] = Field(None, description="Session ID for continuing conversation")
    request_id: Optional[str] = Field(None, description="Request ID for continuing conversation")
    timestamp: str = Field(..., description="Response timestamp")
    status: str = Field(..., description= "current status")
    

class TrainSearchRequest(BaseModel):
    source: str = Field(..., description="Source city/stations")
    destination: str = Field(..., description="Destination city/station")
    date: str = Field(..., description="Date in YYYY-MM-DD format")

class TrainSearchResponse(BaseModel):
    trains: List[Dict[str, Any]]
    source: str
    destination: str
    date: str
    count: int

class FlightSearchRequest(BaseModel):
    from_city: str = Field(..., description="Departure city")
    to_city: str = Field(..., description="Arrival city")
    date: str = Field(..., description="Date in YYYY-MM-DD format")

class FlightSearchResponse(BaseModel):
    flights: List[Dict[str, Any]]
    from_city: str
    to_city: str
    date: str
    count: int

class WebSearchRequest(BaseModel):
    query: str = Field(..., description="Search query")

class WebSearchResponse(BaseModel):
    results: str
    query: str

class DateTimeResponse(BaseModel):
    current_date: str
    current_time: str
    current_day: str
    current_datetime_full: str
    timezone: str

# ================= OPTIMIZED UTILITIES =================

class TTLCache:
    """Thread-safe TTL cache with LRU eviction"""
    def __init__(self, maxsize: int = 128, ttl: int = 3600):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl
        self.lock = threading.Lock()
        self.timestamps = {}
    
    def get(self, key: str) -> Optional[Any]:
        with self.lock:
            if key in self.cache:
                # Check if expired
                if datetime.now().timestamp() - self.timestamps[key] > self.ttl:
                    del self.cache[key]
                    del self.timestamps[key]
                    return None
                # Move to end (LRU)
                self.cache.move_to_end(key)
                return self.cache[key]
            return None
    
    def set(self, key: str, value: Any):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = value
            self.timestamps[key] = datetime.now().timestamp()
            
            # Evict oldest if over maxsize
            if len(self.cache) > self.maxsize:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
                del self.timestamps[oldest_key]
    
    def clear(self):
        with self.lock:
            self.cache.clear()
            self.timestamps.clear()

# Initialize caches
city_code_cache = TTLCache(maxsize=config.CITY_CODE_CACHE_SIZE, ttl=config.CACHE_TTL)
station_info_cache = TTLCache(maxsize=config.STATION_INFO_CACHE_SIZE, ttl=config.CACHE_TTL)
city_correction_cache = TTLCache(maxsize=config.CITY_CORRECTION_CACHE_SIZE, ttl=config.CACHE_TTL)
web_search_cache = TTLCache(maxsize=config.WEB_SEARCH_CACHE_SIZE, ttl=300)  # 5 min for web search

# ================= HTTP SESSION WITH RETRY =================
class OptimizedHTTPSession:
    """Singleton HTTP session with connection pooling and retry logic"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        self.session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=config.MAX_RETRIES,
            backoff_factor=config.BACKOFF_FACTOR,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"]
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )
        
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Default headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36'
        })
    
    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault('timeout', config.REQUEST_TIMEOUT)
        return self.session.get(url, **kwargs)
    
    def post(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault('timeout', config.REQUEST_TIMEOUT)
        return self.session.post(url, **kwargs)

http_session = OptimizedHTTPSession()

# ================= SINGLETON LLM FOR CITY CORRECTION =================
class CorrectionModelSingleton:
    """Singleton pattern for city correction LLM"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        self.model = ChatOllama(
            model=config.MODEL_NAME,
            temperature=config.CORRECTION_MODEL_TEMPERATURE
        )
        logger.info("City correction model initialized")
    
    def get_model(self):
        return self.model

correction_model_singleton = CorrectionModelSingleton()

# ================= OPTIMIZED DATE/TIME FUNCTIONS =================
@lru_cache(maxsize=1)
def get_ist_offset() -> timedelta:
    """Cached IST offset"""
    return timedelta(hours=5, minutes=30)

def current_date_time() -> datetime:
    """Get current date time in IST"""
    return datetime.utcnow() + get_ist_offset()

@lru_cache(maxsize=100)
def parse_date_cached(date_str: str) -> datetime:
    """Cache parsed dates to avoid repeated parsing"""
    return parser.parse(date_str)

def get_current_datetime_info() -> Dict[str, Any]:
    """Get comprehensive current date and time information"""
    now = current_date_time()
    return {
        "current_date": now.strftime("%Y-%m-%d"),
        "current_time": now.strftime("%H:%M:%S"),
        "current_day": now.strftime("%A"),
        "current_datetime_full": now.strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "IST (UTC+5:30)"
    }

@tool
def get_current_time():
    """Get the current date, time, and day information."""
    return get_current_datetime_info()

# ================= OPTIMIZED CITY CORRECTION =================
def correct_city_names(city_name: str) -> str:
    """
    Correct city name spellings with caching.
    Args:
        city_name: Input city name that might have spelling errors
    Returns:
        Corrected city name
    """
    # Check cache first
    cached_result = city_correction_cache.get(city_name.lower())
    if cached_result:
        logger.info(f"City correction cache hit: '{city_name}' → '{cached_result}'")
        return cached_result
    
    try:
        model = correction_model_singleton.get_model()
        
        prompt = f"""
        You are a geographical name correction expert. Your task is to correct and standardize city names.
        
        Input city: "{city_name}"
        
        Instructions:
        1. Correct any spelling mistakes in the city name
        2. Return only the corrected city name in proper case (first letter capitalized)
        3. If it's a well-known city with multiple names, use the most common official name
        4. If the input is unclear or not a valid city, return the most likely correct version
        5. Return ONLY the corrected city name, no explanations
        
        Examples:
        - "mumbay" → "Mumbai"
        - "new delhi" → "New Delhi" 
        - "bangalore" → "Bengaluru"
        - "calcutta" → "Kolkata"
        - "madras" → "Chennai"
        - "puna" → "Pune"
        - "hydrabad" → "Hyderabad"
        - "jaipur" → "Jaipur"
        - "delhi" → "New Delhi"
        
        Corrected city name:
        """
        
        response = model.invoke(prompt)
        corrected_city = response.content.strip()
        
        # Clean up the response
        corrected_city = re.sub(r'["\']', '', corrected_city)
        corrected_city = corrected_city.split('\n')[0].strip()
        
        # Cache the result
        city_correction_cache.set(city_name.lower(), corrected_city)
        
        logger.info(f"City name corrected: '{city_name}' → '{corrected_city}'")
        return corrected_city
        
    except Exception as e:
        logger.error(f"Error in city name correction for '{city_name}': {e}")
        # Fallback: return original name with basic capitalization
        fallback = city_name.title()
        city_correction_cache.set(city_name.lower(), fallback)
        return fallback

# ================= OPTIMIZED API FUNCTIONS =================
def get_station_info(city_name: str) -> tuple:
    """Fetch station information with caching"""
    # Check cache first
    cached_info = station_info_cache.get(city_name.lower())
    if cached_info:
        logger.info(f"Station info cache hit for {city_name}")
        return cached_info
    
    try:
        api_url = f"{config.RAILYATRI_BASE_URL}/api/common_city_station_search.json?q={city_name}&hide_city=true&user_id=-1754124811"
        response = http_session.get(api_url)
        response.raise_for_status()
        data = response.json()
        
        if data.get("success") and data.get("items"):
            first_station = data["items"][0]
            station_info = (
                first_station.get("station_name"),
                first_station.get("station_code")
            )
            # Cache the result
            station_info_cache.set(city_name.lower(), station_info)
            return station_info
        
        return None, None
        
    except Exception as e:
        logger.error(f"Error fetching station info for {city_name}: {e}")
        return None, None

# ================= OPTIMIZED TOOLS =================
@tool
def fetch_bus_data(source: str, destination: str, date: str, base_url: str = "http://127.0.0.1:8003"):
    """
    Fetch bus information from the local FastAPI RedBus server.
    
    Args:
        source (str): Source city name
        destination (str): Destination city name
        date (str): Journey date in YYYY-MM-DD format
        base_url (str): Base URL of the FastAPI server (default: http://127.0.0.1:8003)
        
    Returns:
        list: A list of bus data dictionaries
    """
    endpoint = f"{base_url}/bus_search"
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
        print(f"Successfully fetched {len(data.get('results', []))} buses from API.")
        return data.get("results", [])
    except requests.exceptions.RequestException as e:
        print(f"❌ API request failed: {e}")
        return []

@tool
def search_trains(source: str, destination: str, date: str):
    """
    Search for trains between two cities.
    Args:
        source: Departure city or station name.
        destination: Arrival city or station name.
        date: Travel date in YYYY-MM-DD format.
    Returns:
        JSON string of available trains and details.
    """
    source = correct_city_names(source)
    destination = correct_city_names(destination)
    source_code = get_station_info(source)[1]
    destination_code = get_station_info(destination)[1]
    
    if not source_code or not destination_code:
        logger.error(f"Could not find station codes for source: {source}, destination: {destination}")
        return []
    
    try:
        date_obj = parse_date_cached(date)
        formatted = date_obj.strftime("%Y%m%d")
    except Exception as e:
        logger.error(f"Error parsing date {date}: {e}")
        return []
        
    logger.info(f"Searching trains: {source_code} → {destination_code} on {formatted}")

    url = f"{config.REDBUS_BASE_URL}/rails/api/searchResults"
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
        "origin": config.REDBUS_BASE_URL,
        "referer": config.REDBUS_BASE_URL,
    }

    scraped_data = []

    try:
        response = http_session.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        trains = data.get("trainBtwnStnsList", [])

        for train in trains:
            availability_info = train.get("tbsAvailability", [])

            fares = {}
            for available_class in availability_info:
                class_name = available_class.get("className")
                total_fare = available_class.get("totalFare")
                if class_name and class_name not in fares:
                    fares[class_name] = {
                        "fare": total_fare,
                        "status": available_class.get("availablityStatus"),
                        "seats_available": available_class.get("availablityNumber")
                    }

            scraped_data.append({
                "train_name": train.get("trainName"),
                "train_number": train.get("trainNumber"),
                "departure_station_code": train.get("fromStnCode"),
                "departure_station_name": train.get("fromStnName"),
                "arrival_station_code": train.get("toStnCode"),
                "arrival_station_name": train.get("toStnName"),
                "departure_time": train.get("departureTime"),
                "departure_date": train.get("departureDate"),
                "arrival_time": train.get("arrivalTime"),
                "arrival_date": train.get("arrivalDate"),
                "duration": train.get("duration"),
                "distance": train.get("distance"),
                "available_classes": train.get("avlClasses"),
                "fares_and_availability": fares,
                "running_days": {
                    "sun": train.get("runningSun"),
                    "mon": train.get("runningMon"),
                    "tue": train.get("runningTue"),
                    "wed": train.get("runningWed"),
                    "thu": train.get("runningThu"),
                    "fri": train.get("runningFri"),
                    "sat": train.get("runningSat"),
                }
            })

    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP request error searching trains: {e}")
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from train search response")

    return scraped_data

@tool
def web_search(query: str):
    """Search the web for travel or location-related information with caching."""
    # Check cache first
    cached_result = web_search_cache.get(query.lower())
    if cached_result:
        logger.info(f"Web search cache hit for query: '{query}'")
        return cached_result
    
    logger.info(f"Executing web search with query: '{query}'")
    searcher = SearxSearchWrapper(searx_host=config.SEARX_HOST)
    
    try:
        results = searcher.run(query)
        # Cache the result
        web_search_cache.set(query.lower(), results)
        logger.info(f"Web search returned {len(results)} characters")
        return results
    except Exception as e:
        logger.error(f"Web search failed for query '{query}': {e}")
        return f"Error during web search: {e}"

@tool
def search_flights(from_city: str, to_city: str, date: str):
    """
    Search for flights between two cities.
    Args:
        from_city: Departure city name.
        to_city: Arrival city name.
        date: Travel date in YYYY-MM-DD format.
    Returns:
        JSON string of flight options.
    """
    from_city = correct_city_names(from_city)
    to_city = correct_city_names(to_city)
    
    try:
        date_obj = parse_date_cached(date)
        formatted_date = date_obj.strftime("%d/%m/%Y")
    except Exception as e:
        logger.error(f"Error parsing date {date}: {e}")
        return {"flights": []}
    
    payload = {
        "from_city": from_city,
        "to_city": to_city,
        "date": formatted_date
    }

    try:
        response = http_session.post(config.FLIGHT_API_URL, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error searching flights: {e}")
        return {"flights": []}
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error searching flights: {e}")
        return {"flights": []}

# ================= OPTIMIZED SESSION MANAGEMENT =================
class SessionManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
    
    def get_session(self, session_id: str, user_name: str = None, user_location: str = None):
        with self.lock:
            if session_id not in self.sessions:
                # Create new session
                system_content = "You are a helpful travel assistant."
                if user_name:
                    system_content += f" The user's name is {user_name}."
                if user_location:
                    system_content += f" The user is calling from {user_location}."

                self.sessions[session_id] = {
                    "chat_history": [SystemMessage(content=system_content)],
                    "created_at": datetime.now(),
                    "last_accessed": datetime.now(),
                    "user_name": user_name,
                    "user_location": user_location,
                    "initialized": True
                }
            else:
                # Update existing session
                session = self.sessions[session_id]
                
                # Update user info if provided
                if user_name and not session["user_name"]:
                    session["user_name"] = user_name
                    # Update system message
                    for msg in session["chat_history"]:
                        if isinstance(msg, SystemMessage):
                            if "user's name is" not in msg.content:
                                msg.content += f" The user's name is {user_name}."
                            break
                
                if user_location and not session["user_location"]:
                    session["user_location"] = user_location
                
                session["last_accessed"] = datetime.now()
            
            return self.sessions[session_id]
    
    def cleanup_old_sessions(self, max_age_hours: int = None):
        """Remove sessions older than specified hours"""
        if max_age_hours is None:
            max_age_hours = config.MAX_SESSION_AGE_HOURS
            
        current_time = datetime.now()
        expired_sessions = []
        
        with self.lock:
            for session_id, session_data in list(self.sessions.items()):
                session_age = current_time - session_data["created_at"]
                if session_age.total_seconds() > max_age_hours * 3600:
                    expired_sessions.append(session_id)
            
            for session_id in expired_sessions:
                del self.sessions[session_id]
        
        if expired_sessions:
            logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")
        
        return len(expired_sessions)

session_manager = SessionManager()

# ================= BACKGROUND TASKS =================
async def periodic_session_cleanup():
    """Background task to periodically clean up old sessions"""
    while True:
        await asyncio.sleep(config.SESSION_CLEANUP_INTERVAL)
        try:
            session_manager.cleanup_old_sessions()
        except Exception as e:
            logger.error(f"Error in periodic session cleanup: {e}")

# ================= AGENT INITIALIZATION =================
AGENT_READY = False
agent = None

def initialize_agent():
    """Initialize the travel agent"""
    global agent, AGENT_READY
    
    try:
        model = ChatOllama(
            model=config.MODEL_NAME,
            temperature=config.MODEL_TEMPERATURE,
        )

        agent = create_agent(
            model=model,
            tools=[search_trains, get_current_time, web_search, search_flights, fetch_bus_data],
            system_prompt=("""
            ROLE
            You are Ravina from JS Tech Alliance, a friendly and experienced travel agent helping customers get information about trains, flights, and plan trips over the phone.
            Speak naturally as if you're sitting across from the customer—warm, helpful, and real.

            PRIMARY GOALS
            - Listen carefully, confirm what matters, and offer the best 2–3 choices.
            - Keep responses brief and natural—under 20 words per turn.
            - Sound human: use I'm checking, let me see, I found, looks like, how about.
            - Always end with a genuine question that moves things forward.

            VOICE STYLE
            - Warm, conversational, like a real phone call with a helpful agent.
            - Use natural phrases: "Let me check that," "Give me a moment," "I see," "Perfect," "Got it."
            - Avoid robotic patterns, templates, or formal assistant language.
            - React naturally: "Oh, that date's passed," "Great choice," "Hmm, not seeing much there."
            - Speak as if you're actively looking things up, not instantly retrieving data.

            TOOLS YOU CAN CALL
            1) get_current_time → current date, time, day, timezone.
            2) search_trains(source, destination, date: YYYY‑MM‑DD).
            3) search_flights(from_city, to_city, date: YYYY‑MM‑DD).
            4) web_search(query) for weather, hotels, attractions, tips, visas.
            Never mention these by name—just naturally present what you find.

            ALWAYS DO FIRST
            - Check get_current_time at the start to anchor dates and timezone context.

            INTENT PARSING
            - Train booking: mentions train, rail, or typical train routes.
            - Flight booking: mentions flight, fly, plane, or air travel.
            - General help: weather, hotels, sightseeing, local tips, visa questions.

            INFORMATION TO GATHER
            - Where from and where to; accept nicknames, abbreviations.
            - Travel date: specific or relative (today, tomorrow, Monday).
            - Preferences: time of day, class, direct vs stops.
            - Clarify duplicates naturally: "Mumbai or Mumbai suburbs?"

            DATE HANDLING
            - Convert relative dates using current time context.
            - Past date: "Oh, that's already gone. How about today or tomorrow?"
            - Missing date: "And what day were you thinking?"

            DEFAULTS (when not specified)
            - Show prices in departure city's currency; USD if unclear.
            - Use 12-hour AM/PM format; mirror user's preference.
            - Assume economy for flights, AC/sleeper for trains per region.
            - One-way unless mentioned; ask "Round trip or one-way?"

            TOOL SELECTION
            - Trains → search_trains.
            - Flights → search_flights.
            - Everything else → web_search.
            - Keep searches focused; clarify first if too vague.

            RESULT PRESENTATION (CRITICAL)
            - Never use tables, bullets, lists, or colon formats.
            - Speak naturally: "I'm seeing the Rajdhani at 4 PM for ₹1,200, seats available, or the Shatabdi at 6 AM for ₹950. Which sounds better?"
            - Limit to top 2–3 options in flowing sentences.
            - Include only essentials: train/airline name, time with morning/evening/noon/night, price, duration for trains, direct/stops for flights.
            - Always close with a real question.

            EXAMPLES (NATURAL PHONE STYLE)
            - "Let me check... I'm seeing the Rajdhani leaving at 4 PM evening, 16 hours, ₹1,200. Want that one?"
            - "Got it. IndiGo has an 8:45 AM morning direct flight for ₹5,200."
            - "I found Air India at 7:45 PM evening for ₹9,500, or IndiGo at 10 PM for ₹13,800. Which works?"
            - "Shimla's around 10 to 15°C next week—lovely weather. Need hotel suggestions?"
            - "And which city are you leaving from?"
            - "Perfect. What date did you want to go?"

            CLARIFYING NATURALLY
            - Missing city: "Which city are you starting from?"
            - Missing date: "What day works for you?"
            - Too many results: "I see a few. Morning or evening preference?"

            ERRORS AND FALLBACKS
            - No results: "Hmm, not seeing anything for that time. Want to try earlier or later?"
            - Technical issue: "Sorry, something hiccupped on my end. Let me try again?"
            - Offer alternatives naturally: "How about the next day or a nearby airport?"
                           
            LANGUAGE BEHAVIOR (VERY IMPORTANT)
            - ALWAYS reply in the exact same language the user is currently using.
            - If the user mixes languages (Hinglish), reply in the same mixed style.
            - Never switch language unless the user switches first.
            - Detect the language from the user's latest message ONLY.
        
            SAFETY AND ACCURACY
            - Only share what the tools return; don't guess.
            - Keep it secure—ask only what's needed.
            - For visas and rules, suggest double-checking with official sources.

            INTERNAL PROCESS (NEVER SHOW)
            1) Get current time/date.
            2) Extract cities, date, time preference, class.
            3) If anything's missing, ask naturally before searching.
            4) Run the right tool for the exact date requested.
            5) Pick top options by user preference, time, or price.
            6) Reply in one natural sentence under 20 words with a closing question.

            OUTPUT FORMAT
            - Speak in natural, flowing sentences only.
            - No lists, tables, or structured formats.
            - Under 20 words per response.
            - Always end with a conversational question.
            - Sound like a real person on the phone, not a chatbot.
            
            """),
            middleware=[
                SummarizationMiddleware(
                    model=model,
                    max_tokens_before_summary=1500,
                    messages_to_keep=config.MAX_CHAT_HISTORY
                ),
                TodoListMiddleware(),
                ToolRetryMiddleware(
                    max_retries=2,
                    backoff_factor=1.0,
                    initial_delay=0.5,
                    max_delay=10.0,
                    jitter=True,
                )
            ]
        )
        AGENT_READY = True
        logger.info("Travel agent initialized successfully")
        
    except Exception as e:
        logger.error(f"Failed to initialize travel agent: {e}")
        AGENT_READY = False

# ================= API ROUTES =================
@app.get("/", summary="API Status", response_model=Dict[str, str])
async def root():
    """Check API status"""
    return {
        "status": "active",
        "message": "Travel Assistant API is running",
        "agent_ready": str(AGENT_READY)
    }


# Store ongoing requests
ongoing_requests: Dict[str, dict] = {}

@app.post("/chat", response_model=ChatResponse, summary="Chat with Travel Assistant")
async def chat_with_assistant(request: ChatRequest, background_tasks: BackgroundTasks):
    """Chat with the travel assistant with session support - runs in background"""
    if not AGENT_READY:
        raise HTTPException(
            status_code=503,
            detail="Travel assistant is not ready. Please try again later."
        )
    
    # Generate request ID for tracking
    request_id = str(uuid.uuid4())
    
    # Store initial request data
    ongoing_requests[request_id] = {
        "status": "processing",
        "response": None,
        "error": None,
        "session_id": request.session_id or f"session_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    }
    
    # Add background task
    background_tasks.add_task(process_chat_request, request_id, request)
    
    return ChatResponse(
        response="Your request is being processed. Please check back later.",
        session_id=ongoing_requests[request_id]["session_id"],
        timestamp=datetime.now().isoformat(),
        request_id=request_id,
        status="processing"
    )

async def process_chat_request(request_id: str, request: ChatRequest):
    """Background task to process chat request"""
    try:
        # Generate session ID if not provided
        session_id = request.session_id or f"session_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        # Get session data
        session_data = session_manager.get_session(
            session_id,
            user_name=request.user_name,
            user_location=request.user_location
        )
        chat_history = session_data["chat_history"]
        
        # Add user message
        chat_history.append(HumanMessage(content=request.message))
        
        # Invoke the agent
        import time
        start_time = time.time()
        
        try:
            result = agent.invoke({"messages": chat_history})
            processing_time = time.time() - start_time
            logger.info(f"Agent processed request in {processing_time:.2f}s")
            
            # Extract response (your existing logic)
            if hasattr(result, 'content'):
                reply = result.content
            elif isinstance(result, dict) and 'output' in result:
                reply = result['output']
            elif isinstance(result, dict) and 'messages' in result:
                messages = result['messages']
                if messages and hasattr(messages[-1], 'content'):
                    reply = messages[-1].content
                else:
                    reply = str(messages[-1]) if messages else "I'm not sure how to respond."
            else:
                reply = str(result) if result else "I didn't get a response."
                
        except Exception as agent_error:
            logger.error(f"Agent invocation error: {agent_error}")
            reply = "I'm having trouble processing your request right now. Please try again."
        
        # Add AI response to history
        chat_history.append(AIMessage(content=reply))
        
        # Limit history size
        if len(chat_history) > config.MAX_CHAT_HISTORY:
            system_msgs = [msg for msg in chat_history if isinstance(msg, SystemMessage)]
            recent_msgs = chat_history[-(config.MAX_CHAT_HISTORY - len(system_msgs)):]
            chat_history = system_msgs + recent_msgs
        
        session_data["chat_history"] = chat_history
        
        # Update the request with result
        ongoing_requests[request_id].update({
            "status": "completed",
            "response": reply,
            "session_id": session_id
        })
        
    except Exception as e:
        logger.error(f"Error in background chat processing: {e}")
        ongoing_requests[request_id].update({
            "status": "error",
            "error": str(e)
        })

@app.get("/chat/status/{request_id}")
async def get_chat_status(request_id: str):
    """Check status of a background chat request"""
    if request_id not in ongoing_requests:
        raise HTTPException(status_code=404, detail="Request not found")
    
    return ongoing_requests[request_id]

@app.get("/chat/result/{request_id}")
async def get_chat_result(request_id: str):
    """Get the result of a completed chat request"""
    if request_id not in ongoing_requests:
        raise HTTPException(status_code=404, detail="Request not found")
    
    request_data = ongoing_requests[request_id]
    
    if request_data["status"] == "processing":
        raise HTTPException(status_code=425, detail="Request still processing")
    elif request_data["status"] == "error":
        raise HTTPException(status_code=500, detail=f"Processing failed: {request_data['error']}")
    
    return ChatResponse(
        response=request_data["response"],
        session_id=request_data["session_id"],
        timestamp=datetime.now().isoformat(),
        request_id=request_id,
        status="completed"
    )

@app.post("/search/trains", response_model=TrainSearchResponse, summary="Search for Trains")
async def search_trains_endpoint(request: TrainSearchRequest):
    """Search for trains between source and destination on a specific date"""
    try:
        trains = search_trains.invoke({
            "source": request.source,
            "destination": request.destination,
            "date": request.date
        })
        
        return TrainSearchResponse(
            trains=trains,
            source=request.source,
            destination=request.destination,
            date=request.date,
            count=len(trains)
        )
        
    except Exception as e:
        logger.error(f"Error in train search: {e}")
        raise HTTPException(status_code=500, detail=f"Train search failed: {str(e)}")

@app.post("/search/flights", response_model=FlightSearchResponse, summary="Search for Flights")
async def search_flights_endpoint(request: FlightSearchRequest):
    """Search for flights between cities on a specific date"""
    try:
        flights_data = search_flights.invoke({
            "from_city": request.from_city,
            "to_city": request.to_city,
            "date": request.date
        })
        
        flights = flights_data.get("flights", []) if isinstance(flights_data, dict) else flights_data or []
            
        return FlightSearchResponse(
            flights=flights,
            from_city=request.from_city,
            to_city=request.to_city,
            date=request.date,
            count=len(flights)
        )
        
    except Exception as e:
        logger.error(f"Error in flight search: {e}")
        raise HTTPException(status_code=500, detail=f"Flight search failed: {str(e)}")

@app.post("/search/web", response_model=WebSearchResponse, summary="Perform Web Search")
async def web_search_endpoint(request: WebSearchRequest):
    """Perform web search for travel-related information"""
    try:
        results = web_search.invoke({"query": request.query})
        
        return WebSearchResponse(
            results=results,
            query=request.query
        )
        
    except Exception as e:
        logger.error(f"Error in web search: {e}")
        raise HTTPException(status_code=500, detail=f"Web search failed: {str(e)}")

@app.get("/datetime", response_model=DateTimeResponse, summary="Get Current Date and Time")
async def get_current_datetime():
    """Get current date and time information in IST"""
    try:
        return DateTimeResponse(**get_current_datetime_info())
    except Exception as e:
        logger.error(f"Error getting datetime: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get current datetime: {str(e)}")

@app.post("/sessions/cleanup", summary="Cleanup Expired Sessions")
async def cleanup_sessions():
    """Manually trigger session cleanup"""
    try:
        cleaned_count = session_manager.cleanup_old_sessions()
        return {
            "message": "Session cleanup completed",
            "cleaned_sessions": cleaned_count,
            "active_sessions": len(session_manager.sessions)
        }
    except Exception as e:
        logger.error(f"Error in session cleanup: {e}")
        raise HTTPException(status_code=500, detail=f"Session cleanup failed: {str(e)}")

@app.post("/cache/clear", summary="Clear All Caches")
async def clear_caches():
    """Clear all caches"""
    try:
        city_code_cache.clear()
        station_info_cache.clear()
        city_correction_cache.clear()
        web_search_cache.clear()
        
        # Clear LRU caches
        get_ist_offset.cache_clear()
        parse_date_cached.cache_clear()
        
        return {
            "message": "All caches cleared successfully"
        }
    except Exception as e:
        logger.error(f"Error clearing caches: {e}")
        raise HTTPException(status_code=500, detail=f"Cache clear failed: {str(e)}")

@app.get("/health", summary="Health Check")
async def health_check():
    """Comprehensive health check"""
    try:
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "agent_ready": AGENT_READY,
            "components": {
                "model": "available" if AGENT_READY else "unavailable",
                "tools": "available",
                "sessions": f"{len(session_manager.sessions)} active",
                "caches": {
                    "city_codes": len(city_code_cache.cache),
                    "station_info": len(station_info_cache.cache),
                    "city_corrections": len(city_correction_cache.cache),
                    "web_search": len(web_search_cache.cache)
                }
            }
        }
        
        return health_status
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail="Service unhealthy")

@app.get("/stats", summary="Get API Statistics")
async def get_stats():
    """Get API statistics"""
    return {
        "sessions": {
            "active": len(session_manager.sessions),
            "total_created": len(session_manager.sessions)
        },
        "caches": {
            "city_codes": {
                "size": len(city_code_cache.cache),
                "max_size": config.CITY_CODE_CACHE_SIZE
            },
            "station_info": {
                "size": len(station_info_cache.cache),
                "max_size": config.STATION_INFO_CACHE_SIZE
            },
            "city_corrections": {
                "size": len(city_correction_cache.cache),
                "max_size": config.CITY_CORRECTION_CACHE_SIZE
            },
            "web_search": {
                "size": len(web_search_cache.cache),
                "max_size": config.WEB_SEARCH_CACHE_SIZE
            }
        },
        "agent_ready": AGENT_READY,
        "config": {
            "max_session_age_hours": config.MAX_SESSION_AGE_HOURS,
            "max_chat_history": config.MAX_CHAT_HISTORY,
            "cache_ttl": config.CACHE_TTL
        }
    }

class ReqSession(BaseModel):
    sessionId: str
    query: Optional[str] = None
    user_name: str
    user_location: str


class ResSession(ReqSession):
    status: str
    result: str

class ApiSessionManager:
    _instance = None

    def __init__(self):
        self.sessionMap: Dict[str, ResSession] = {}

    def getInstance():
        if ApiSessionManager._instance is None:
            ApiSessionManager._instance = ApiSessionManager()
        return ApiSessionManager._instance
    
    def getSessionById(self, id: str):
        return self.sessionMap.get(id)
    
    def addSession(self, ses: ResSession):
        self.sessionMap[ses.sessionId] = ses
        return self

def fetch_travel_data(api_session: ResSession):
    """Background function to fetch travel data"""
    # Implement your travel data fetching logic here
    try:
        # Simulate data processing
        time.sleep(2)
        api_session.status = "completed"
        api_session.result = "Travel data fetched successfully"
    except Exception as e:
        api_session.status = "error"
        api_session.result = f"Error: {str(e)}"

@app.post("/api/session")
async def call_session_by_id(body: ReqSession):
    apiSessionManager = ApiSessionManager.getInstance()
    apiSession = apiSessionManager.getSessionById(body.sessionId)
    
    if apiSession:
        return apiSession
    
    # Create new session with initial status
    apiSession = ResSession(
        **body.dict(),
        status="processing",
        result=""
    )
    apiSessionManager.addSession(apiSession)
    
    # Start background data fetching
    import threading
    thread = threading.Thread(target=fetch_travel_data, args=(apiSession,))
    thread.daemon = True
    thread.start()
    
    return apiSession

# ================= STARTUP AND SHUTDOWN =================
@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    logger.info("Travel Assistant API starting up...")
    initialize_agent()
    # Start background cleanup task
    # asyncio.create_task(periodic_session_cleanup())

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown"""
    logger.info("Travel Assistant API shutting down...")
    session_manager.cleanup_old_sessions(max_age_hours=0)
    
    # Clear all caches
    city_code_cache.clear()
    station_info_cache.clear()
    city_correction_cache.clear()
    web_search_cache.clear()

if __name__ == "__main__":
    uvicorn.run(
        "travel_assistant:app",
        host=config.HOST,
        port=config.PORT,
        reload=True,
        log_level="info"
    )