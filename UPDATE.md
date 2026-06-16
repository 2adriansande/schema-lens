# SchemaLens — AI-Powered Schema Extraction & SQL Learning Platform

> **Version 2.0** — Secure Gemini backend integration, interactive SQL Compiler, isolated practice database.

---

## Table of Contents

1. [Overview](#overview)
2. [What's New in v2.0](#whats-new)
3. [Architecture](#architecture)
4. [Setup & Installation](#setup)
5. [Gemini API Backend Configuration](#gemini-config)
6. [SQL Compiler](#sql-compiler)
7. [Sample Database Schema](#sample-database)
8. [Security Model](#security)
9. [Environment Variables Reference](#env-reference)
10. [User Guide](#user-guide)
11. [Project Structure](#project-structure)

---

## Overview

SchemaLens is a Flask web application that helps students and developers:
- **Extract database schemas** from ERD images using OCR + Gemini Vision
- **Generate SQL queries** from natural language using AI
- **Learn SQL** interactively with lessons, challenges, and badges
- **Practice SQL** in a safe, isolated SQL Compiler environment

---

## What's New in v2.0

### Secure Gemini Backend Integration
- The Gemini API key is loaded exclusively from **server-side environment variables**
- The key is **never sent to the browser**, never appears in API responses, never exposed in developer tools
- Clean `_get_effective_gemini_key()` service layer: server env key takes priority over per-user DB key
- Rate limiting (1-second minimum between calls), structured logging, and model fallback chain

### SQL Compiler
- Full-featured SQL editor (JetBrains Mono font, Tab support, Ctrl+Enter to run)
- Executes against an **isolated in-memory SQLite** practice database (fresh per request — no state leaks)
- Supports SELECT, INSERT, UPDATE, DELETE, JOIN, GROUP BY, HAVING, ORDER BY, Subqueries, Aggregates
- Execution time and row count display, color-coded status indicators (Ready / Running / Success / Error)
- AI SQL Assistant powered by Gemini backend: Explain / Fix / Generate modes

### Practice Database
- 5 related tables: students, courses, enrollments, departments, professors
- Realistic Filipino university sample data
- Schema explorer with column types, PK/NN indicators, row counts, one-click table preview
- 12 built-in sample queries covering all major SQL operations

### SQL Security
- Blocked commands: DROP, TRUNCATE, ALTER, ATTACH, DETACH, PRAGMA, VACUUM
- Queries execute in a **completely isolated in-memory database** — application DB is never touched
- Table name validation via strict regex for preview endpoint
- Fresh SQLite connection per request — no cross-user state

---

## Architecture

```
Browser (Frontend)
  Templates: Jinja2 + Bootstrap Icons + JetBrains Mono
  NO API keys, NO Gemini credentials exposed here
         |
         | HTTP
         v
Flask Application (app.py)
  +------------------+   +---------------------------+
  | Gemini Service   |   | SQL Compiler Service      |
  | _call_gemini()   |   | _execute_practice_sql()   |
  | Rate limiting    |   | _get_practice_schema()    |
  | Logging          |   | Isolated :memory: SQLite  |
  | Fallback chain   |   | Security validation       |
  +------------------+   +---------------------------+
         |
  Environment Variables (.env)
    GEMINI_API_KEY  <- server-side only, NEVER sent to frontend
    SECRET_KEY
    OLLAMA_BASE_URL
```

### SQL Compiler Request Flow
```
User types SQL
  -> POST /api/sql-compiler/execute
  -> Regex check: block forbidden keywords (DROP, TRUNCATE, etc.)
  -> Fresh :memory: SQLite connection (isolated from schemalens.db)
  -> Seed PRACTICE_DB_SCHEMA + PRACTICE_DB_SEED
  -> Execute user SQL
  -> Return {columns, rows, rowcount, duration_ms, error}
  -> Render results table in browser
```

---

## Setup & Installation

### Prerequisites
- Python 3.11+

### Steps

```bash
# 1. Enter the project directory
cd schema-lens-enhanced

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Linux / macOS
venv\Scripts\activate           # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — add your GEMINI_API_KEY

# 5. Run
python app.py
# Open http://localhost:5000
```

---

## Gemini API Backend Configuration

### Getting a Key
1. Go to https://aistudio.google.com/app/apikey
2. Sign in with Google, click **Create API key**
3. Copy the key (starts with AIza...)

### Configuring the Key
Edit `.env`:
```
GEMINI_API_KEY=AIzaSyYour_actual_key_here
```
Restart Flask. The key is loaded at startup via python-dotenv.

**NEVER** put the API key in any template, JS file, or API response.

### Key Resolution Priority
```
_get_effective_gemini_key(user_key):
  1. GEMINI_API_KEY env var  <- preferred (server-wide)
  2. user.gemini_key (DB)    <- per-user fallback
  3. Empty -> AI features return 503 with clear error
```

### Rate Limiting & Logging
- 1-second minimum interval between calls (_GEMINI_MIN_INTERVAL)
- All calls logged at INFO level with model name and outcome
- Model fallback chain: gemini-2.5-flash -> gemini-2.0-flash -> gemini-1.5-flash

---

## SQL Compiler

### Accessing It
Click **SQL Compiler** in the left sidebar, or navigate to `/sql-compiler`.

### Layout
```
+------------------+------------------------------------------+
| Schema Explorer  |  SQL Editor (JetBrains Mono)             |
|                  |                                          |
| [students]       |  SELECT s.first_name, c.course_name      |
|  - student_id PK |  FROM students s                         |
|  - first_name    |  JOIN enrollments e ON ...               |
|  - last_name     |  [Run Query]  [Clear]  [AI Help]         |
| [courses]        +------------------------------------------+
| [enrollments]    |  Results Table                           |
| [departments]    |  v Success  3 rows  12ms                 |
| [professors]     |  first_name | course_name | grade        |
|                  |  John       | Database... | 1.50         |
| Quick Queries    |  Maria      | Web Dev     | 1.75         |
| [Reset DB]       |  Kevin      | Data Struc  | 1.25         |
+------------------+------------------------------------------+
```

### Keyboard Shortcuts
- Ctrl+Enter: Run query
- Tab: Insert 2 spaces

### Supported SQL Operations
- Query: SELECT, WHERE, ORDER BY, LIMIT, DISTINCT
- Joins: INNER JOIN, LEFT JOIN, CROSS JOIN
- Aggregation: COUNT, SUM, AVG, MIN, MAX, GROUP BY, HAVING
- Subqueries: Scalar, column, table subqueries
- DML: INSERT, UPDATE, DELETE
- Multi-statement: Multiple statements separated by semicolon

### AI SQL Assistant
Click AI Help to open the panel:
- **Explain**: Explains the current query in plain English for beginners
- **Fix Error**: Paste an error message, get corrected SQL
- **Generate**: Describe what you want, get SQL generated with one-click "Load into Editor"

All AI calls go to `/api/sql-compiler/ai-help` on the backend. The Gemini key never appears in the browser network tab.

---

## Sample Database Schema

The practice database is seeded fresh for every query execution.

### students
| Column | Type | Notes |
|--------|------|-------|
| student_id | INTEGER | PK |
| first_name | TEXT | NOT NULL |
| last_name | TEXT | NOT NULL |
| course | TEXT | NOT NULL (BSCS, BSIT, BSIS) |
| year_level | INTEGER | Default 1 |
| gpa | REAL | Default 0.0 |
| email | TEXT | UNIQUE |

### courses
| Column | Type | Notes |
|--------|------|-------|
| course_id | INTEGER | PK |
| course_code | TEXT | UNIQUE NOT NULL |
| course_name | TEXT | NOT NULL |
| department | TEXT | NOT NULL |
| units | INTEGER | Default 3 |
| professor | TEXT | |

### enrollments
| Column | Type | Notes |
|--------|------|-------|
| enrollment_id | INTEGER | PK |
| student_id | INTEGER | FK -> students |
| course_id | INTEGER | FK -> courses |
| grade | REAL | Philippine grading (1.0=best, 5.0=fail) |
| semester | TEXT | |
| school_year | TEXT | |

### departments
| Column | Type | Notes |
|--------|------|-------|
| dept_id | INTEGER | PK |
| dept_code | TEXT | UNIQUE NOT NULL |
| dept_name | TEXT | NOT NULL |
| dean | TEXT | |
| building | TEXT | |

### professors
| Column | Type | Notes |
|--------|------|-------|
| prof_id | INTEGER | PK |
| first_name | TEXT | NOT NULL |
| last_name | TEXT | NOT NULL |
| dept_id | INTEGER | FK -> departments |
| email | TEXT | UNIQUE |
| specialization | TEXT | |

---

## Security Model

### Gemini API Key Security
- Stored in server environment only (GEMINI_API_KEY in .env)
- Never serialized in any JSON response
- Never referenced in any JavaScript file
- Never logged (key value is never printed)
- Per-user keys in DB are fallback only; server key is always preferred

### SQL Compiler Security
- **Isolated execution**: every query runs in a fresh :memory: SQLite — completely separate from schemalens.db
- **Forbidden keyword blocking**: DROP, TRUNCATE, ALTER, ATTACH, DETACH, PRAGMA, VACUUM blocked by regex before execution
- **Table name whitelisting**: preview endpoint validates names with ^[a-zA-Z_][a-zA-Z0-9_]*$ before executing
- **No persistent writes**: in-memory database is destroyed after each request
- **No cross-user state**: each request gets a brand-new connection and fresh seed data

### General Application Security
- Passwords hashed with scrypt via Werkzeug
- CSRF protection via Flask session secret key
- File uploads validated by extension allowlist + 16MB size limit
- All data endpoints protected by @login_required
- Application DB queries use SQLAlchemy parameterized queries

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| SECRET_KEY | Yes | dev key | Flask session secret. Generate: python -c "import secrets; print(secrets.token_hex(32))" |
| GEMINI_API_KEY | Recommended | "" | Server-side Gemini API key. NEVER sent to frontend. |
| OLLAMA_BASE_URL | No | http://localhost:11434 | Ollama local LLM endpoint |

---

## User Guide

### Running Your First SQL Query
1. Log in and click **SQL Compiler** in the sidebar
2. The schema explorer on the left shows all available tables and their columns
3. Click any table name to expand and see column details
4. Click **Preview data** to auto-load a SELECT * LIMIT 10 query
5. Or click any Quick Query in the bottom-left panel
6. Edit the query in the editor and press Run Query or Ctrl+Enter
7. Results appear at the bottom with row count and execution time

### Using AI Help
1. Write or load a SQL query into the editor
2. Click AI Help (top right of toolbar)
3. Choose Explain, Fix Error, or Generate mode
4. All AI calls are processed server-side — your Gemini key is never visible in the browser

### About the Reset Button
The practice database is reset automatically with every query (each execution gets a fresh in-memory database). Clicking Reset Database clears the editor and confirms the reset state visually.

---

## Project Structure

```
schema-lens-enhanced/
├── app.py                   # Main Flask application
│   ├── Gemini service       #   _call_gemini(), _get_effective_gemini_key()
│   ├── SQL Compiler routes  #   /sql-compiler, /api/sql-compiler/*
│   └── Practice DB engine   #   _execute_practice_sql(), PRACTICE_DB_SCHEMA/SEED
├── .env.example             # Environment template (copy to .env)
├── .env                     # Your config (DO NOT commit to git)
├── requirements.txt         # Python dependencies
├── schemalens.db            # Application SQLite database
├── static/
│   ├── css/dashboard.css
│   └── uploads/             # User-uploaded ERD images
└── templates/
    ├── base.html            # Sidebar nav (includes SQL Compiler link)
    ├── sql_compiler.html    # SQL Compiler page (NEW in v2.0)
    ├── settings.html        # Updated with Gemini key security notice
    └── ...other templates
```

### Key Backend Functions

| Function | Purpose |
|----------|---------|
| _get_effective_gemini_key() | Resolves API key: env var > user DB |
| _call_gemini() | Gemini API with rate limiting, logging, model fallback |
| _execute_practice_sql() | Runs SQL in isolated in-memory SQLite |
| _get_practice_db() | Creates fresh seeded practice database connection |
| _get_practice_schema_info() | Returns table/column metadata for schema explorer |
| sql_compiler() | Renders /sql-compiler page |
| sql_compiler_execute() | POST /api/sql-compiler/execute |
| sql_compiler_ai_help() | POST /api/sql-compiler/ai-help |
| sql_compiler_preview() | GET /api/sql-compiler/preview/<table> |

---

## Changelog

### v2.0
- Gemini API moved to secure server-side backend with .env configuration
- Added _get_effective_gemini_key() service layer (env key > user DB key)
- Added rate limiting and structured logging for all Gemini calls
- Added SQL Compiler with isolated in-memory SQLite practice database
- Added 5-table sample schema (students, courses, enrollments, departments, professors)
- Added schema explorer, table preview, quick queries, reset database
- Added AI SQL Assistant (explain/fix/generate) via Gemini backend
- Added SQL security: forbidden keyword blocking, table name whitelisting
- Updated Settings page with Gemini key security notice
- Added SQL Compiler nav link in sidebar
- Added .env.example template
- Updated requirements.txt with python-dotenv and google-genai

### v1.x
- Flask + EasyOCR schema extraction
- Gemini + Ollama dual-model SQL generation  
- Challenges, badges, leaderboard system
- SQL Learn module
