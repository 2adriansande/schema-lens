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
* Gemini API configuration
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

## Gemini API Configuration

1. Generate a Gemini API key through Google AI Studio.
2. Sign in to SchemaLens.
3. Navigate to:

```text
Settings → Gemini API Key
```

4. Enter and save your API key.

### Fallback Mode

If a Gemini API key is not provided, SchemaLens automatically switches to:

* OCR-based heuristic schema parsing
* Rule-based SQL generation templates

The application remains operational, although AI-assisted query generation may have reduced accuracy.

---

## Project Structure

```text
schemalens/
├── app.py
├── requirements.txt
├── schemalens.db
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

| Path             | Description                      |
| ---------------- | -------------------------------- |
| app.py           | Main Flask application           |
| requirements.txt | Project dependencies             |
| schemalens.db    | SQLite database (auto-generated) |
| static/uploads   | Uploaded schema images           |
| templates        | User interface templates         |

---

## Database Models

### User

Stores account and profile information.

| Field      |
| ---------- |
| id         |
| name       |
| email      |
| password   |
| company    |
| role       |
| gemini_key |
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

## License

This project was developed for academic and educational purposes.

