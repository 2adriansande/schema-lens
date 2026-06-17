# SchemaLens

**AI-Powered Web-Based System for Intelligent SQL Query Generation Using Natural Language Processing and Automated Database Schema Image Recognition**

SchemaLens enables users to upload database schema images (ER diagrams), automatically extract and analyze their structure using Optical Character Recognition (OCR) and Artificial Intelligence (AI), and generate SQL queries from natural language descriptions.

---

## Overview

SchemaLens is designed to bridge the gap between database schema interpretation and SQL query development. By combining OCR technology, machine learning, and natural language processing, the system simplifies database understanding and accelerates SQL query generation for students, developers, analysts, and researchers.

---

## Key Features

### User Authentication

* Secure user registration and login
* Password encryption using Werkzeug hashing
* Session management with Flask-Login
* Account management and profile settings

### Database Schema Upload

* Upload Entity-Relationship Diagrams (ERDs) and database schema images
* Supported file formats:

  * PNG
  * JPG / JPEG
  * BMP
  * WEBP

### OCR-Based Schema Extraction

* Automated text extraction using EasyOCR
* Image preprocessing with OpenCV to improve recognition accuracy
* Identification of schema elements from uploaded diagrams

### AI-Powered Schema Analysis

Utilizes Google Gemini 1.5 Flash to analyze extracted schema information and identify:

* Tables
* Columns
* Primary Keys
* Foreign Keys
* Relationships between entities

### Natural Language to SQL Generation

Converts plain English requests into SQL queries based on the detected database schema.

#### Example

Input:

```text
Show all customers who placed orders this month
```

Generated SQL:

```sql
SELECT *
FROM customers c
JOIN orders o ON c.customer_id = o.customer_id
WHERE MONTH(o.order_date) = MONTH(CURRENT_DATE);
```

### Query History Management

* Stores generated SQL queries
* Tracks query generation timestamps
* Records AI model information
* Provides historical query access for users

### User Settings

* Profile management
* Account updates
* Account deletion

### Safe by Design

SchemaLens does not execute generated SQL statements.

The system is intended solely for SQL generation, learning, analysis, and development purposes, ensuring that uploaded databases remain unaffected.

---

## Technology Stack

| Layer          | Technology                         |
| -------------- | ---------------------------------- |
| Backend        | Python, Flask                      |
| Frontend       | HTML, CSS, Bootstrap 5, JavaScript |
| Database       | SQLite, SQLAlchemy ORM             |
| Authentication | Flask-Login, Werkzeug              |
| OCR            | EasyOCR, OpenCV                    |
| AI Model       | Google Gemini 2.5 Flash            |

---

## Installation Guide

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/schemalens.git
cd schemalens
```

### 2. Create a Virtual Environment

```bash
python -m venv venv
```

Activate the virtual environment:

**Windows**

```bash
venv\Scripts\activate
```

**Linux / macOS**

```bash
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the Application

```bash
python app.py
```

Access the application through:

```text
http://localhost:5000
```

---

## Gemini API Configuration (Developer-Only)

The Gemini API key is configured **once, by the developer or operator**, and is never entered, viewed, or managed by end users anywhere in the application.

### Setup

1. Generate a Gemini API key through Google AI Studio: https://aistudio.google.com/app/apikey
2. Copy `.env.example` to `.env` (if you haven't already).
3. Set the key in `.env`:

```text
GEMINI_API_KEY=your_actual_api_key_here
```

4. Restart the application. The backend reads `GEMINI_API_KEY` from the environment on startup via `os.environ.get("GEMINI_API_KEY", "")`, and every Gemini request is made server-side using this key. The key is never sent to the browser, never returned in any API response, never stored in a user session, and never saved in the database.

### Old vs. New Flow

**Old flow (removed):**
1. User creates an account.
2. User enters their own Gemini API key.
3. User saves the API key to their profile.
4. User uses AI features.

**New flow:**
1. Developer configures `GEMINI_API_KEY` in `.env`.
2. User creates an account.
3. User logs in.
4. User immediately uses AI features — no API key input required at any point.

### Fallback Mode

If `GEMINI_API_KEY` is not configured, SchemaLens automatically switches to:

* OCR-based heuristic schema parsing
* Rule-based SQL generation templates
* Local Ollama models, where available, for SQL generation and tutoring

The application remains fully operational without a Gemini key, though AI-assisted image parsing and some advanced features will be unavailable. The dashboard reflects this with a generic "AI Vision" status indicator — it never exposes whether or how the key is configured beyond that simple on/off state.

---

## Project Structure

```text
schemalens/
├── app.py
├── requirements.txt
├── schemalens.db
├── .env.example
├── .gitignore
├── migrations/
│   └── remove_gemini_key_column.py
├── static/
│   └── uploads/
│
└── templates/
    ├── base.html
    ├── login.html
    ├── signup.html
    ├── dashboard.html
    ├── schemas.html
    ├── schema_detail.html
    ├── query.html
    ├── history.html
    └── settings.html
```

### Directory Description

| Path                                      | Description                                                  |
| ------------------------------------------ | -------------------------------------------------------------- |
| app.py                                    | Main Flask application                                        |
| requirements.txt                          | Project dependencies                                           |
| schemalens.db                             | SQLite database (auto-generated)                              |
| .env.example                              | Environment variable template (copy to `.env`, never commit `.env`) |
| migrations/remove_gemini_key_column.py    | Standalone migration removing the legacy per-user API key column |
| static/uploads                            | Uploaded schema images                                        |
| templates                                 | User interface templates                                      |

---

## Database Models

### User

Stores account and profile information. The Gemini API key is **not** part of this model — it lives only in the server-side `GEMINI_API_KEY` environment variable.

| Field      |
| ---------- |
| id         |
| name       |
| email      |
| password   |
| company    |
| role       |
| created_at |

### SchemaUpload

Stores uploaded schema images and extracted metadata.

| Field                |
| -------------------- |
| id                   |
| user_id              |
| filename             |
| original_name        |
| schema_name          |
| ocr_text             |
| parsed_schema (JSON) |
| uploaded_at          |

### QueryHistory

Stores generated SQL queries and related information.

| Field         |
| ------------- |
| id            |
| user_id       |
| schema_id     |
| natural_query |
| generated_sql |
| model_used    |
| created_at    |

---

## Intended Users

SchemaLens is designed for:

* Students learning SQL and database concepts
* Database administrators
* Data analysts
* Software developers
* System analysts
* Researchers working with database schemas

---

## Security Notes

* The Gemini API key lives only in the server process's environment (`GEMINI_API_KEY`) and is read once at startup.
* No template, route, or JSON response ever includes the key value.
* No form, input field, or button in the UI lets a user submit, edit, or view an API key.
* The key is never written to the database, a user session, or application logs.
* If `GEMINI_API_KEY` is missing, AI-dependent endpoints return a generic "temporarily unavailable" message to users; the specific reason is only recorded in server-side logs.

---

## License

This project was developed for academic and educational purposes.

