
# AI Travel Agent Server

FastAPI backend for an AI travel assistant built with LangChain and LangGraph. It can help users search for trains, buses and flights; perform web lookups; and persist session-level memory (user name, location, conversation history) in PostgreSQL.

This README explains how to set up and run the project locally, how the main pieces fit together, and how to troubleshoot common issues (Ollama connectivity, database setup, sessions).

---

## Features

- Conversation-driven travel assistant powered by an LLM (via Ollama / ChatOllama).
- Tools for train, bus and flight search (connects to local microservices and web APIs).
- Persistent memory using LangGraph + Postgres (stores `messages` channel and `user_profiles`).
- Session management (session IDs) and optional session resume when a returning user provides the same `session_id`.

## Quickstart (local development)

### Prerequisites

- Python 3.10+
- PostgreSQL server
- Optional: Ollama running locally (default local API at `http://localhost:11434`) or another supported LLM backend

### 1. Clone the repository

```bash
git clone https://github.com/aayush584/travel_assistant.git
cd travel_agent
```

### 2. Create and activate a virtualenv

```bash
python -m venv botenv
source botenv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

> If `requirements.txt` is missing, install the main packages used by the project:

```bash
pip install fastapi uvicorn langchain langchain-ollama langchain-community langgraph psycopg psycopg_pool requests python-dateutil toon-format
```

### 4. Configure environment variables (recommended)

Create a `.env` or export these in your shell. At minimum set the Postgres URI and Ollama host if using a local Ollama:

```bash
export DB_URI='postgresql://<user>:<password>@localhost:5432/<database>'
# If you run Ollama locally, set the host to the local API
export OLLAMA_API_URL='http://localhost:11434'
```

Alternatively edit `travel_agent.py` to change `DB_URI` or the model `ChatOllama(...)` options.

### 5. Run the server

```bash
python travel_agent.py
# or
uvicorn travel_agent:app --host 0.0.0.0 --port 8004 --reload
```

On startup the app will:
- initialize the DB connection pool and create the `user_profiles` table (if missing)
- create/check LangGraph checkpoint tables via `PostgresSaver.setup()`

## API (selected endpoints)

- `POST /session/create` — create a new session. Returns `session_id`.
- `POST /chat` — main chat endpoint. Body: `{ "message": "...", "session_id": "optional-uuid" }`.
- `GET /session/{session_id}/history` — returns stored conversation history for the session (from LangGraph checkpoint storage).
- `PUT /session/{session_id}/user-info` — manually update user info for a session.
- `GET /sessions` — list active sessions (message counts, created_at).

Use the auto-generated OpenAPI docs at `http://localhost:8004/docs` when the server is running.

## Sessions and user persistence

- The agent collects customer name and location and calls `save_user_info` to upsert into `user_profiles`.
- The server stores a `session_id` (UUID). If the same `session_id` is used again, the server will attempt to load `user_profiles` and populate session context so Ravina recognizes returning users.

If you want long-lived user recognition independent of `session_id`, add a user identifier (email/phone) and store it in the DB along with session mapping.

## Troubleshooting


### 1) Database tables (checkpoints / user_profiles) missing

On first startup the app calls `PostgresSaver.setup()` to create LangGraph tables. If you see errors like `relation "checkpoints" does not exist`, confirm the DB user has permission to create tables or run the `setup()` migration manually.

### 2) Messages or memory not appearing

- The app persists `messages` into LangGraph Postgres saver after each chat turn. If you don't see messages in history, check logs for persistence errors and verify `checkpoints` table content.

## Development notes

- The project uses a hybrid approach: `psycopg_pool.AsyncConnectionPool` for application DB operations and a synchronous `psycopg.Connection` for the LangGraph `PostgresSaver` checkpointer. This was chosen to match the saver API but can be replaced with a pool-based saver for higher throughput.
- The system prompt for the LLM is configured to force collection of `name` and `location` and to call the `save_user_info` tool immediately once those are available.

## Contributing

If you'd like to contribute, please open a PR against the `main` branch with a clear description. Typical improvements:
- Use pool-backed Postgres saver or async saver for non-blocking writes
- Add authentication and user-to-session mapping (email/phone)
- Improve tool wrappers and error handling for external microservices

