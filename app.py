import os
import json
import re
import datetime
import random
import time
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load environment variables from .env file (if present)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

# ── Supabase Auth ──────────────────────────────────────────────────────────
try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
    logger_pre = logging.getLogger("schemalens")
    logger_pre.warning("supabase-py not installed. Run: pip install supabase")
from sqlalchemy import text
import urllib.request

# ── App Setup ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "schemalens-dev-secret-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{BASE_DIR / 'schemalens.db'}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = "llama3.2:3b"

# ── Gemini Backend Configuration (server-side only) ────────────────────────
# API key is NEVER sent to the frontend. It is loaded from environment only.
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS   = ["gemini-2.5-flash", "gemini-3.0-flash"]

# ── Supabase Configuration ─────────────────────────────────────────────────
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()

supabase = None
_supabase_init_error = None

if not _SUPABASE_AVAILABLE:
    _supabase_init_error = "supabase-py package is not installed (pip install supabase)"
elif not SUPABASE_URL or not SUPABASE_ANON_KEY:
    _supabase_init_error = "SUPABASE_URL and/or SUPABASE_ANON_KEY missing from environment/.env"
else:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    except Exception as _e:
        _supabase_init_error = f"create_client() failed: {_e}"

if _supabase_init_error:
    # Use print as well since this runs before logging is configured
    print(f"[SchemaLens] WARNING: Supabase auth disabled — {_supabase_init_error}")
else:
    print(f"[SchemaLens] Supabase auth ENABLED — project: {SUPABASE_URL}")

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("schemalens")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

# ── Models ─────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id            = db.Column(db.Integer, primary_key=True)
    supabase_uid  = db.Column(db.String(256), unique=True, nullable=True)  # Supabase Auth UID
    name          = db.Column(db.String(120), nullable=False)
    email         = db.Column(db.String(200), unique=True, nullable=False)
    password      = db.Column(db.String(256), nullable=True)  # nullable — Supabase owns credentials
    company       = db.Column(db.String(120), default="")
    role          = db.Column(db.String(80), default="")
    last_active   = db.Column(db.DateTime, nullable=True)
    streak_days   = db.Column(db.Integer, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    agreement_accepted    = db.Column(db.Boolean, default=False)
    agreement_accepted_at = db.Column(db.DateTime, nullable=True)
    schemas      = db.relationship("SchemaUpload", backref="owner", lazy=True,
                                   cascade="all, delete-orphan")
    queries      = db.relationship("QueryHistory", backref="owner", lazy=True,
                                   cascade="all, delete-orphan")
    
    badges       = db.relationship("UserBadge", backref="user", lazy=True,
                                   cascade="all, delete-orphan")
    challenges   = db.relationship("ChallengeAttempt", backref="user", lazy=True,
                                   cascade="all, delete-orphan")


class SchemaUpload(db.Model):
    __tablename__ = "schema_uploads"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    filename      = db.Column(db.String(256), nullable=False)
    original_name = db.Column(db.String(256), nullable=False)
    schema_name   = db.Column(db.String(120), default="")
    ocr_text      = db.Column(db.Text, default="")
    parsed_schema = db.Column(db.Text, default="")   # JSON
    confirmed_schema = db.Column(db.Text, default="")  # JSON — user-confirmed/edited version
    uploaded_at   = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    queries       = db.relationship("QueryHistory", backref="schema", lazy=True,
                                    cascade="all, delete-orphan")


class QueryHistory(db.Model):
    __tablename__ = "query_history"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    schema_id      = db.Column(db.Integer, db.ForeignKey("schema_uploads.id"), nullable=True)
    natural_query  = db.Column(db.Text, nullable=False)
    generated_sql  = db.Column(db.Text, default="")
    model_used     = db.Column(db.String(80), default="")
    sql_explanation = db.Column(db.Text, default="")     # Feature 2: plain-English explanation
    accuracy_score  = db.Column(db.Float, default=None)  # Feature 4: confidence score
    accuracy_notes  = db.Column(db.Text, default="")     # Feature 4: validation notes
    conversation_context = db.Column(db.Text, default="")  # Feature 3: JSON conversation history
    created_at     = db.Column(db.DateTime, default=datetime.datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _sync_user_from_supabase(local_user, sb_user):
    """
    Make Supabase the source of truth for identity fields (name, email).
    Updates the local row in place if Supabase has different/newer values.
    Does not touch app-data fields (schemas, queries, badges, etc.) — those
    only ever live in SQLite.
    """
    changed = False

    sb_email = getattr(sb_user, "email", None)
    if sb_email and local_user.email != sb_email:
        local_user.email = sb_email
        changed = True

    sb_metadata = getattr(sb_user, "user_metadata", None) or {}
    sb_name = sb_metadata.get("name")
    if sb_name and local_user.name != sb_name:
        local_user.name = sb_name
        changed = True

    if changed:
        db.session.commit()
    return local_user


# ── DB Migration: add new columns if missing ───────────────────────────────
def run_migrations():
    """Add new columns to existing tables without destroying data."""
    with db.engine.connect() as conn:
        migrations = [
            ("schema_uploads", "confirmed_schema", "TEXT DEFAULT ''"),
            ("users", "agreement_accepted", "INTEGER DEFAULT 0"),
            ("users", "agreement_accepted_at", "DATETIME"),
            ("users", "last_active",  "DATETIME"),
            ("users", "streak_days",  "INTEGER DEFAULT 0"),
            ("query_history", "sql_explanation", "TEXT DEFAULT ''"),
            ("query_history", "accuracy_score", "REAL"),
            ("query_history", "accuracy_notes", "TEXT DEFAULT ''"),
            ("query_history", "conversation_context", "TEXT DEFAULT ''"),
            ("challenge_attempts", "difficulty", "TEXT DEFAULT 'easy'"),
            ("users", "supabase_uid", "TEXT"),  # Supabase Auth UID
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
            except Exception:
                pass  # Column already exists

        # ── Drop legacy per-user Gemini API key column, if present ─────────
        # The Gemini API key is now managed exclusively via the server-side
        # GEMINI_API_KEY environment variable. Older databases may still have
        # a `gemini_key` column from before this change — remove it so no
        # API key material lingers in the database.
        try:
            conn.execute(text("ALTER TABLE users DROP COLUMN gemini_key"))
            conn.commit()
        except Exception:
            pass  # Column already absent, or SQLite version doesn't support DROP COLUMN


# ── Template Context ──────────────────────────────────────────────────────
@app.context_processor
def inject_ai_status():
    """
    Expose only a boolean flag indicating whether AI-powered (Gemini) features
    are available — never the key itself, and never anything user-configurable.
    Templates can use `ai_vision_available` to adjust messaging, but no
    template should ever request or display an API key.
    """
    return {"ai_vision_available": bool(GEMINI_API_KEY.strip())}


# ── Jinja Filters ──────────────────────────────────────────────────────────
@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


@app.template_filter('ph_time')
def ph_time_filter(dt):
    """Convert UTC datetime to Philippines time (UTC+8) for display."""
    if not dt:
        return ''
    if isinstance(dt, str):
        try:
            dt = datetime.datetime.fromisoformat(dt)
        except Exception:
            return dt
    # Assume UTC if no timezone
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    ph_tz = datetime.timezone(datetime.timedelta(hours=8))
    dt_ph = dt.astimezone(ph_tz)
    return dt_ph.strftime('%b %d, %Y %I:%M %p')


# ── Helpers ────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ════════════════════════════════════════════════════════════════════════════
# OCR — returns structured bounding-box data, NOT flattened text
# ════════════════════════════════════════════════════════════════════════════

def run_ocr(image_path: str) -> str:
    """
    Run EasyOCR and return a structured spatial representation preserving
    bounding-box positions.  Each line of output is:
        x1,y1,x2,y2|text|confidence
    This lets downstream code reconstruct the visual layout.
    Falls back to plain text if EasyOCR is unavailable.
    """
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = reader.readtext(image_path, detail=1, paragraph=False)
        out = []
        for bbox, text, conf in results:
            if conf < 0.1 or not text.strip():
                continue
            # bbox is [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
            x1 = int(min(p[0] for p in bbox))
            y1 = int(min(p[1] for p in bbox))
            x2 = int(max(p[0] for p in bbox))
            y2 = int(max(p[1] for p in bbox))
            out.append(f"{x1},{y1},{x2},{y2}|{text.strip()}|{conf:.3f}")
        return "\n".join(out)
    except Exception as exc:
        return f"[OCR error: {exc}]"


def _ocr_is_spatial(ocr_text: str) -> bool:
    """Return True if ocr_text is in our structured x1,y1,x2,y2|text|conf format."""
    if not ocr_text or ocr_text.startswith("[OCR error"):
        return False
    first = ocr_text.strip().splitlines()[0]
    parts = first.split("|")
    if len(parts) < 2:
        return False
    coords = parts[0].split(",")
    return len(coords) == 4 and all(c.strip().lstrip("-").isdigit() for c in coords)


def _parse_spatial_ocr(ocr_text: str) -> list[dict]:
    """
    Parse our structured OCR format into a list of token dicts:
      { x1, y1, x2, y2, cx, cy, w, h, text, conf }
    Sorted top-to-bottom, then left-to-right.
    """
    tokens = []
    for line in ocr_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        coords = parts[0].split(",")
        if len(coords) != 4:
            continue
        try:
            x1, y1, x2, y2 = int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3])
        except ValueError:
            continue
        text = parts[1].strip()
        conf = float(parts[2]) if len(parts) > 2 else 1.0
        if not text:
            continue
        tokens.append({
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
            "w": x2 - x1, "h": y2 - y1,
            "text": text, "conf": conf,
        })
    tokens.sort(key=lambda t: (t["y1"], t["x1"]))
    return tokens


def _cluster_tokens_into_boxes(tokens: list[dict], gap_threshold: float = 0.8) -> list[list[dict]]:
    """
    Group OCR tokens into visual "boxes" (table containers) using spatial proximity.

    Strategy:
    1. Sort tokens by Y position.
    2. Estimate a typical "row height" from the median token height.
    3. A new cluster starts whenever the Y gap between consecutive tokens
       exceeds gap_threshold × median_row_height.
    4. Within a cluster, tokens are sub-grouped into rows by Y proximity,
       then rows are sorted left-to-right.

    Each cluster represents one table box from the diagram.
    """
    if not tokens:
        return []

    # Estimate row height
    heights = sorted(t["h"] for t in tokens)
    median_h = heights[len(heights) // 2] if heights else 20
    gap = max(gap_threshold * median_h, 8)

    # Split into vertical clusters
    clusters: list[list[dict]] = []
    current: list[dict] = [tokens[0]]
    for tok in tokens[1:]:
        prev = current[-1]
        y_gap = tok["y1"] - prev["y2"]
        if y_gap > gap:
            clusters.append(current)
            current = [tok]
        else:
            current.append(tok)
    if current:
        clusters.append(current)

    # Within each cluster, group into rows
    result = []
    for cluster in clusters:
        if not cluster:
            continue
        # Sort by y1 then x1
        cluster.sort(key=lambda t: (t["y1"], t["x1"]))
        # Sub-group by row (tokens within median_h/2 of each other vertically)
        rows: list[list[dict]] = []
        cur_row: list[dict] = [cluster[0]]
        for tok in cluster[1:]:
            if abs(tok["cy"] - cur_row[-1]["cy"]) < median_h * 0.8:
                cur_row.append(tok)
            else:
                rows.append(sorted(cur_row, key=lambda t: t["x1"]))
                cur_row = [tok]
        rows.append(sorted(cur_row, key=lambda t: t["x1"]))
        # Flatten rows back to token list (already in reading order)
        result.append([t for row in rows for t in row])

    return result


def _box_to_table(box_tokens: list[dict]) -> dict | None:
    """
    Convert a cluster of spatially-grouped OCR tokens into a table definition.

    Layout rules:
    - Row 0 (topmost) = table header / table name
    - Each subsequent row = one column definition
    - Within a column row, the LEFT token is the column name,
      the RIGHT token(s) are the data type + modifiers (PK, FK, NOT NULL…)
    - Data type tokens that appear alone in a row are attached to the
      previous column (OCR sometimes splits them across lines).
    """
    if not box_tokens:
        return None

    # Re-group into rows (tokens at same Y ± threshold)
    rows: list[list[dict]] = []
    cur_row: list[dict] = [box_tokens[0]]
    heights = [t["h"] for t in box_tokens]
    median_h = sorted(heights)[len(heights) // 2] if heights else 16
    for tok in box_tokens[1:]:
        if abs(tok["cy"] - cur_row[-1]["cy"]) < median_h * 0.75:
            cur_row.append(tok)
        else:
            rows.append(sorted(cur_row, key=lambda t: t["x1"]))
            cur_row = [tok]
    rows.append(sorted(cur_row, key=lambda t: t["x1"]))

    if not rows:
        return None

    # ── Row 0: table name ──────────────────────────────────────────────────
    header_tokens = rows[0]
    table_name_raw = " ".join(t["text"] for t in header_tokens).strip()
    # Strip surrounding punctuation / brackets
    table_name = re.sub(r'^[\[\({<]+|[\]\)}>]+$', '', table_name_raw).strip()
    # Reject if header looks like a data type
    if table_name.upper() in _SQL_TYPES_SET:
        return None
    # Must look like a valid identifier (allow spaces for "User Account" style)
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_ ]*$', table_name):
        return None

    # ── Rows 1+: column definitions ────────────────────────────────────────
    columns: list[dict] = []
    pending_col: dict | None = None   # waiting for a type token on the next row

    for row in rows[1:]:
        row_texts = [t["text"].strip() for t in row]
        row_texts_up = [s.upper() for s in row_texts]

        # ── Pure type row (single token that IS a SQL type) ─────────────
        # Attach to pending column from the previous row
        if len(row_texts) == 1 and row_texts_up[0] in _SQL_TYPES_SET:
            if pending_col is not None:
                pending_col["type"] = row_texts_up[0]
                columns.append(pending_col)
                pending_col = None
            # else: stray type token — ignore
            continue

        # Flush any pending column without a type
        if pending_col is not None:
            pending_col["type"] = "TEXT"
            columns.append(pending_col)
            pending_col = None

        # ── Normal column row ────────────────────────────────────────────
        # First non-type token = column name; remaining = type / modifiers
        col_name = None
        col_type = "TEXT"
        is_pk = False
        is_nullable = True
        is_fk = False

        # Find column name (leftmost token that isn't a pure type keyword)
        for i, token_up in enumerate(row_texts_up):
            raw = row_texts[i]
            if token_up not in _SQL_TYPES_SET and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', raw):
                col_name = raw
                # Everything to the right is type / modifier
                for j in range(i + 1, len(row_texts)):
                    part_up = row_texts_up[j]
                    if part_up in _SQL_TYPES_SET and part_up not in {"PK", "FK", "NOT", "NULL",
                                                                       "PRIMARY", "FOREIGN",
                                                                       "KEY", "UNIQUE",
                                                                       "REFERENCES"}:
                        col_type = part_up
                    if part_up in ("PK", "PRIMARY"):
                        is_pk = True
                    if part_up in ("FK", "FOREIGN"):
                        is_fk = True
                    if part_up == "NOT":
                        is_nullable = False
                break

        if col_name is None:
            # All tokens are type/modifier — pure type row misidentified; skip
            continue

        col_def: dict = {
            "name": col_name,
            "type": col_type,
            "primary_key": is_pk,
            "nullable": is_nullable,
        }
        if is_fk:
            col_def["foreign_key"] = True

        # Auto-detect PK: first column named "id" or ending in "_id"
        if not columns and (col_name.lower() == "id" or col_name.lower().endswith("_id")):
            col_def["primary_key"] = True
            col_def["nullable"] = False

        # If no type found yet, keep as pending (type may appear on next row)
        if col_type == "TEXT" and len(row_texts) == 1:
            pending_col = col_def
        else:
            columns.append(col_def)

    # Flush final pending
    if pending_col is not None:
        pending_col["type"] = "TEXT"
        columns.append(pending_col)

    if not columns:
        return None

    return {"name": table_name, "columns": columns}


def _infer_relationships_spatially(tables: list[dict]) -> list[dict]:
    """
    Infer FK relationships by matching column names of the form <table>_id
    to actual table names in the schema.
    Handles common plural/singular mismatches: user_id → users, product_id → products.
    """
    relationships: list[dict] = []
    # Build lookup: exact name AND common variations
    name_map: dict[str, str] = {}
    for t in tables:
        n = t["name"].lower()
        name_map[n] = t["name"]               # exact: "users"
        if n.endswith("s"):
            name_map[n[:-1]] = t["name"]      # singular: "user"
        else:
            name_map[n + "s"] = t["name"]     # plural: "orders"
        if n.endswith("ies"):
            name_map[n[:-3] + "y"] = t["name"]   # categories → category

    for table in tables:
        for col in table.get("columns", []):
            cname = col["name"].lower()
            if cname.endswith("_id") and not col.get("primary_key"):
                ref_key = cname[:-3]    # strip "_id"
                ref_table_name = name_map.get(ref_key)
                if ref_table_name and ref_table_name != table["name"]:
                    # Avoid duplicate relationships
                    dup = any(
                        r["from_table"] == table["name"] and r["from_col"] == col["name"]
                        for r in relationships
                    )
                    if not dup:
                        relationships.append({
                            "from_table": table["name"],
                            "from_col": col["name"],
                            "to_table": ref_table_name,
                            "to_col": "id",
                            "type": "many-to-one",
                        })
    return relationships


# ════════════════════════════════════════════════════════════════════════════
# Gemini helpers
# ════════════════════════════════════════════════════════════════════════════

def _clean_json_response(text: str) -> str:
    """Strip markdown code fences and whitespace from a Gemini JSON response."""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```\s*$', '', text, flags=re.MULTILINE)
    return text.strip()


# ════════════════════════════════════════════════════════════════════════════
# CORE: Image-first schema parsing via Gemini Vision
# ════════════════════════════════════════════════════════════════════════════

# Forbidden table names — data type tokens must NEVER become table names
_SQL_TYPES_SET = {
    "UUID", "STRING", "STR", "INT", "INTEGER", "FLOAT", "DOUBLE", "BOOLEAN",
    "BOOL", "DATE", "TEXT", "VARCHAR", "CHAR", "TIMESTAMP", "DATETIME",
    "BIGINT", "SMALLINT", "DECIMAL", "NUMERIC", "REAL", "BLOB", "JSON",
    "ARRAY", "SERIAL", "AUTO_INCREMENT", "AUTOINCREMENT", "NULL", "NOT",
    "PRIMARY", "FOREIGN", "KEY", "REFERENCES", "DEFAULT", "UNIQUE",
    "INDEX", "CONSTRAINT", "PK", "FK",
}


SCHEMA_VISION_PROMPT = """You are an expert database schema analyst.

TASK: Analyze this database ER diagram image and extract the exact schema.

═══════════════════════════════════════════════════════════════
VISUAL STRUCTURE RULES — follow these exactly:
═══════════════════════════════════════════════════════════════

1. TABLES come from VISUAL BOXES/CONTAINERS in the diagram.
   • The HEADER ROW at the TOP of each box = the table name.
   • Body rows INSIDE the box = column definitions.
   • Never infer a table from a standalone word or data type token.

2. COLUMNS come from the BODY ROWS inside each box.
   • Left side of a row = column name
   • Right side of a row = data type abbreviation (UUID, STRING, INT, etc.)
   • Preserve original column names exactly as written.

3. PRIMARY KEYS: detect by explicit "PK" label, or first column named "id" / "*_id".

4. FOREIGN KEYS: detect by explicit "FK" label, or column named "<other_table>_id".

5. RELATIONSHIPS: detect connector lines/arrows between boxes.
   • Line from Box A to Box B = relationship between those tables.
   • Infer FK column from the line's endpoint inside the box.

6. FORBIDDEN TABLE NAMES — the following values are DATA TYPES, never table names:
   UUID, STRING, INT, INTEGER, FLOAT, DOUBLE, BOOLEAN, DATE, TEXT, VARCHAR,
   CHAR, TIMESTAMP, BIGINT, DECIMAL, BOOL, REAL, BLOB, JSON, NULL

7. VALIDATION before output:
   • Every table must have a valid header identifier (not a type keyword).
   • Every column must have both a name and a data type.
   • Data types are attached to columns — they are NEVER promoted to table names.
   • Remove any tables where the name is a data type keyword.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT — return ONLY this JSON, no markdown, no text:
═══════════════════════════════════════════════════════════════
{
  "tables": [
    {
      "name": "exact_table_name_from_box_header",
      "columns": [
        {"name": "column_name", "type": "DATA_TYPE", "primary_key": true, "nullable": false},
        {"name": "another_col", "type": "STRING",    "primary_key": false, "nullable": true}
      ]
    }
  ],
  "relationships": [
    {
      "from_table": "orders",
      "from_col":   "user_id",
      "to_table":   "users",
      "to_col":     "user_id",
      "type":       "many-to-one"
    }
  ]
}

Respond with ONLY the JSON object. No markdown fences. No preamble. No explanation."""


SCHEMA_TEXT_PROMPT_TEMPLATE = """You are an expert database schema analyst.

TASK: Reconstruct a database schema from OCR tokens extracted from an ER diagram.
The tokens are listed with their bounding-box coordinates so you can infer visual grouping.

FORMAT OF EACH TOKEN LINE:
  x1,y1,x2,y2|text|confidence
  (x1,y1 = top-left corner;  x2,y2 = bottom-right corner)

═══════════════════════════════════════════════════════════════
SPATIAL RECONSTRUCTION RULES:
═══════════════════════════════════════════════════════════════
1. Tokens with similar X ranges AND close Y positions belong to the SAME table box.
2. The topmost token (or topmost row) in a visual box = TABLE NAME (header).
3. Each subsequent row in the same box = one COLUMN DEFINITION.
4. Within a column row, LEFT token = column name; RIGHT token = data type.
5. Tokens that appear as the ONLY token in a row AND are data type keywords
   are data types attached to the PREVIOUS column — never table names.

FORBIDDEN TABLE NAMES — these are DATA TYPES, never table names:
UUID, STRING, INT, INTEGER, FLOAT, DOUBLE, BOOLEAN, DATE, TEXT, VARCHAR,
CHAR, TIMESTAMP, BIGINT, DECIMAL, BOOL, REAL, BLOB, JSON, NULL

VALIDATION:
• Every detected table must have a valid identifier name (not a type keyword).
• Every column must have a name and a type.
• Remove any table whose name is a forbidden keyword.

═══════════════════════════════════════════════════════════════
OUTPUT: return ONLY this JSON structure, no markdown, no explanation:
═══════════════════════════════════════════════════════════════
{{
  "tables": [
    {{
      "name": "table_name",
      "columns": [
        {{"name": "col_name", "type": "DATA_TYPE", "primary_key": true/false, "nullable": true/false}}
      ]
    }}
  ],
  "relationships": [
    {{"from_table": "t1", "from_col": "c1", "to_table": "t2", "to_col": "c2", "type": "one-to-many"}}
  ]
}}

OCR TOKENS:
{ocr_tokens}

Return ONLY the JSON. No markdown fences. No explanation."""


MODELS = GEMINI_MODELS

# ── Rate limiting state for Gemini ────────────────────────────────────────
_gemini_last_call: float = 0.0
_GEMINI_MIN_INTERVAL: float = 1.0  # seconds between calls

def _get_gemini_key() -> str:
    """
    Return the backend-configured Gemini API key.

    The key is managed exclusively via the GEMINI_API_KEY environment
    variable, set by the developer/operator. It is never sourced from
    user input, session data, or the database, and is never returned
    to the frontend.
    """
    return GEMINI_API_KEY.strip()


def _call_gemini(api_key: str, contents) -> str:
    """
    Call the Gemini API with rate limiting, logging, and error handling.
    api_key: resolved by caller via _get_gemini_key().
    """
    global _gemini_last_call

    if not api_key:
        raise ValueError("No Gemini API key configured. Set GEMINI_API_KEY in .env or server environment.")

    # Enforce minimum interval between calls
    elapsed = time.time() - _gemini_last_call
    if elapsed < _GEMINI_MIN_INTERVAL:
        time.sleep(_GEMINI_MIN_INTERVAL - elapsed)

    logger.info("Calling Gemini API (model loop: %s)", GEMINI_MODELS)

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        for model_name in GEMINI_MODELS:
            try:
                response = client.models.generate_content(model=model_name, contents=contents)
                _gemini_last_call = time.time()
                logger.info("Gemini API success via google-genai, model=%s", model_name)
                return response.text
            except Exception as e:
                logger.warning("Gemini model %s failed (google-genai): %s", model_name, e)
    except ImportError:
        pass
    except Exception as e:
        logger.warning("google-genai client error: %s", e)

    # Legacy fallback
    try:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        for model_name in GEMINI_MODELS:
            try:
                model = genai_legacy.GenerativeModel(model_name)
                response = model.generate_content(contents)
                _gemini_last_call = time.time()
                logger.info("Gemini API success via google-generativeai, model=%s", model_name)
                return response.text
            except Exception as e:
                logger.warning("Gemini model %s failed (legacy): %s", model_name, e)
    except ImportError:
        pass

    raise RuntimeError("No working Gemini model found. Check GEMINI_API_KEY and installed packages.")


def _validate_schema_structure(data: dict) -> None:
    """
    Ensure the parsed schema has the correct shape.
    Removes any 'table' whose name is a SQL type keyword.
    Raises ValueError on structural problems.
    """
    if not isinstance(data, dict):
        raise ValueError("Response is not a dict")
    if "tables" not in data:
        raise ValueError("Missing 'tables' key")
    if not isinstance(data["tables"], list):
        raise ValueError("'tables' must be a list")

    cleaned = []
    for t in data["tables"]:
        if not isinstance(t, dict) or "name" not in t:
            continue
        name = str(t["name"]).strip()
        # Hard reject: table name is a forbidden data-type keyword
        if name.upper() in _SQL_TYPES_SET:
            continue
        # Must be a valid identifier (spaces allowed for multi-word names)
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_ ]*$', name):
            continue
        # Normalise columns list
        if not isinstance(t.get("columns"), list):
            t["columns"] = []
        # Remove columns whose name is a type keyword
        t["columns"] = [
            c for c in t["columns"]
            if isinstance(c, dict)
            and c.get("name")
            and str(c["name"]).upper() not in _SQL_TYPES_SET
            and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', str(c["name"]))
        ]
        cleaned.append(t)

    data["tables"] = cleaned
    if "relationships" not in data:
        data["relationships"] = []


def parse_schema_with_image(image_path: str, api_key: str) -> dict:
    """
    PRIMARY path: send the image directly to Gemini Vision.
    Vision sees the actual boxes, headers, column rows, and connector lines.
    """
    import PIL.Image
    img = PIL.Image.open(image_path)
    raw_text = _call_gemini(api_key, [SCHEMA_VISION_PROMPT, img])
    raw = _clean_json_response(raw_text)
    result = json.loads(raw)
    _validate_schema_structure(result)
    return result


def parse_schema_with_gemini(ocr_text: str, api_key: str, image_path: str = None) -> dict:
    """
    Orchestrator: tries image Vision → spatial OCR text → plain OCR text.
    Each stage validates and rejects hallucinated type-as-table errors.
    """
    # ── Stage 1: Gemini Vision (image) ──────────────────────────────────────
    if image_path and os.path.exists(image_path):
        try:
            result = parse_schema_with_image(image_path, api_key)
            if result.get("tables"):
                return result
        except Exception:
            pass  # fall through to next stage

    # ── Stage 2: Gemini with spatial OCR tokens ──────────────────────────────
    # If ocr_text is in our structured format, send it as coordinate-annotated tokens
    if _ocr_is_spatial(ocr_text):
        try:
            prompt = SCHEMA_TEXT_PROMPT_TEMPLATE.format(ocr_tokens=ocr_text)
            raw_text = _call_gemini(api_key, prompt)
            raw = _clean_json_response(raw_text)
            result = json.loads(raw)
            _validate_schema_structure(result)
            if result.get("tables"):
                return result
        except Exception:
            pass

    # ── Stage 3: Gemini with plain OCR text ──────────────────────────────────
    plain_text = ocr_text
    if _ocr_is_spatial(ocr_text):
        # Extract just the text tokens from structured format
        plain_text = "\n".join(
            line.split("|")[1].strip()
            for line in ocr_text.strip().splitlines()
            if "|" in line
        )
    try:
        prompt = SCHEMA_TEXT_PROMPT_TEMPLATE.format(ocr_tokens=plain_text)
        raw_text = _call_gemini(api_key, prompt)
        raw = _clean_json_response(raw_text)
        result = json.loads(raw)
        _validate_schema_structure(result)
        return result
    except Exception as exc:
        raise RuntimeError(f"All Gemini parsing stages failed: {exc}")


# ── PARALLEL #3: Multi-model consensus ────────────────────────────────────

def parse_schema_with_consensus(ocr_text: str, api_key: str, image_path: str = None) -> dict:
    """
    Fan out schema parsing to Gemini + Ollama simultaneously.
    Returns whichever result has more tables; falls back gracefully if either fails.
    This is a replicated parallel pattern — both models race and the best wins.
    """
    def _gemini_parse():
        return parse_schema_with_gemini(ocr_text, api_key, image_path)

    def _ollama_parse():
        return parse_schema_fallback(ocr_text)

    results = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_gemini_parse): "gemini",
            pool.submit(_ollama_parse): "ollama",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception:
                results[name] = {"tables": [], "relationships": []}

    gemini_tables = len(results.get("gemini", {}).get("tables", []))
    ollama_tables  = len(results.get("ollama",  {}).get("tables", []))

    # Pick the result with more tables; prefer Gemini on a tie
    if gemini_tables >= ollama_tables and gemini_tables > 0:
        return results["gemini"]
    elif ollama_tables > 0:
        return results["ollama"]
    else:
        raise RuntimeError("Both Gemini and Ollama returned no tables.")


# ── Ollama helpers ─────────────────────────────────────────────────────────

def _call_ollama(prompt: str, system: str = "") -> str:
    """Call the local Ollama API with llama3.2:3b and return the response text."""
    import json as _json
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = _json.loads(resp.read().decode("utf-8"))
    return result.get("response", "").strip()


def check_ollama_active() -> bool:
    """Return True if the Ollama server is reachable and the model is available."""
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/tags",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        return any(OLLAMA_MODEL in m for m in models)
    except Exception:
        return False


# ── SQL generation via Ollama ──────────────────────────────────────────────

def generate_sql_with_ollama(natural_query: str, schema: dict) -> str:
    """Generate SQL from natural language using local Ollama (llama3.2:3b)."""
    schema_desc = _build_schema_description(schema)
    system = (
        "You are an expert SQL assistant. "
        "Generate only valid SQL queries. "
        "Return ONLY the SQL statement — no markdown, no backticks, no explanation."
    )
    prompt = f"""{schema_desc}

User Request: {natural_query}

Generate a single, correct SQL query that satisfies the user's request.
Use standard SQL syntax. Use table aliases where appropriate.
Return ONLY the SQL query."""
    raw = _call_ollama(prompt, system=system)
    # Strip accidental markdown fences
    raw = re.sub(r'^```(?:sql)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```\s*$', '', raw, flags=re.MULTILINE)
    return raw.strip()


def _build_schema_description(schema: dict) -> str:
    schema_desc = "Database Schema:\n"
    for table in schema.get("tables", []):
        cols = ", ".join(
            f"{c['name']} {c.get('type','')}{' PK' if c.get('primary_key') else ''}"
            for c in table.get("columns", [])
        )
        schema_desc += f"  Table {table['name']}: ({cols})\n"
    for rel in schema.get("relationships", []):
        schema_desc += (
            f"  Relationship: {rel['from_table']}.{rel['from_col']} → "
            f"{rel['to_table']}.{rel['to_col']} ({rel.get('type', '')})\n"
        )
    return schema_desc


# ── Heuristic fallback parser (no API key) ─────────────────────────────────

# Alias so existing code that references _SQL_TYPES still works
_SQL_TYPES = _SQL_TYPES_SET


def parse_schema_fallback(ocr_text: str) -> dict:
    """
    Spatial heuristic parser for ER diagram OCR text.
    Works with BOTH our structured coordinate format and plain text.

    When structured (x1,y1,x2,y2|text|conf):
      - Clusters tokens by visual proximity into table boxes
      - Within each box, first row = table name; subsequent rows = columns
      - Left token in a row = column name; right token = data type

    When plain text:
      - Single-word lines not matching SQL types = table headers
      - Lines with "name TYPE" pattern = column definitions
      - "Type on next line" pattern is also handled
    """
    if _ocr_is_spatial(ocr_text):
        return _spatial_fallback(ocr_text)
    else:
        return _text_fallback(ocr_text)


def _spatial_fallback(ocr_text: str) -> dict:
    """Parse structured bounding-box OCR into schema using spatial clustering."""
    tokens = _parse_spatial_ocr(ocr_text)
    if not tokens:
        return {"tables": [], "relationships": []}

    boxes = _cluster_tokens_into_boxes(tokens)
    tables: list[dict] = []
    for box in boxes:
        table = _box_to_table(box)
        if table and table.get("columns"):
            tables.append(table)

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[dict] = []
    for t in tables:
        if t["name"] not in seen:
            seen.add(t["name"])
            unique.append(t)

    relationships = _infer_relationships_spatially(unique)
    return {"tables": unique, "relationships": relationships}


def _text_fallback(ocr_text: str) -> dict:
    """
    State-machine parser for plain-text OCR output.

    Rules:
    - A line is a TABLE HEADER if it is a single identifier word AND not a type keyword
    - A line is a COLUMN if it has pattern:  name TYPE [modifiers]
    - A lone TYPE token on its own line attaches to the previous column
    """
    lines = [l.strip() for l in ocr_text.splitlines()]
    tables: list[dict] = []
    current: dict | None = None
    pending_name: str | None = None   # column name waiting for its type

    def flush():
        nonlocal current
        if current and current.get("columns"):
            tables.append(current)
        current = None

    def new_table(name: str):
        nonlocal current, pending_name
        flush()
        pending_name = None
        current = {"name": name, "columns": []}

    def add_col(name: str, col_type: str, is_pk: bool = False, nullable: bool = True):
        nonlocal pending_name
        pending_name = None
        if current is None:
            return
        # Auto PK: first col named id / *_id
        if not current["columns"] and (name.lower() == "id" or name.lower().endswith("_id")):
            is_pk = True
            nullable = False
        current["columns"].append({
            "name": name,
            "type": col_type,
            "primary_key": is_pk,
            "nullable": nullable,
        })

    for line in lines:
        if not line:
            pending_name = None
            continue

        parts = line.split()
        if not parts:
            continue

        first = parts[0].strip()
        first_up = first.upper()

        # ── Pending: type on next line ───────────────────────────────────
        if pending_name and len(parts) == 1 and first_up in _SQL_TYPES_SET:
            add_col(pending_name, first_up)
            pending_name = None
            continue

        if pending_name:
            # Broken pending — flush with TEXT type
            add_col(pending_name, "TEXT")
            pending_name = None

        # Skip lone type tokens
        if first_up in _SQL_TYPES_SET and len(parts) == 1:
            continue

        # Skip non-identifier starts
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', first):
            continue

        # ── Column line: "name TYPE [modifiers]" ────────────────────────
        if len(parts) >= 2 and parts[1].upper() in _SQL_TYPES_SET:
            col_type = parts[1].upper()
            is_pk = any(p.upper() in ("PK", "PRIMARY") for p in parts[2:])
            nullable = not any(p.upper() == "NOT" for p in parts[2:])
            add_col(first, col_type, is_pk, nullable)
            continue

        # ── Table header: single word, not a type keyword ────────────────
        if first_up not in _SQL_TYPES_SET and len(parts) == 1:
            new_table(first)
            continue

        # ── Ambiguous single-word identifier with no type yet ────────────
        if len(parts) == 1 and current:
            pending_name = first
            continue

        # ── Multi-word line without a recognised type token ──────────────
        # Treat as a new table header if it looks like one
        if first_up not in _SQL_TYPES_SET and not current:
            new_table(first)

    flush()

    # Deduplicate
    seen: set[str] = set()
    unique: list[dict] = []
    for t in tables:
        if t["name"] not in seen and t.get("columns"):
            seen.add(t["name"])
            unique.append(t)

    relationships = _infer_relationships_spatially(unique)
    return {"tables": unique, "relationships": relationships}


def _fallback_sql(query: str, schema: dict) -> str:
    """Generate a basic SQL template when no API key is available."""
    q = query.lower()
    tables = [t["name"] for t in schema.get("tables", [])]
    first_table = tables[0] if tables else "your_table"

    # Try to find the most relevant table by name mention in the query
    for tname in tables:
        if tname.lower() in q:
            first_table = tname
            break

    if "count" in q:
        return f"SELECT COUNT(*) FROM {first_table};"
    if "all" in q or "list" in q or "show" in q or "get" in q or "select" in q:
        return f"SELECT * FROM {first_table} LIMIT 100;"
    if "delete" in q:
        return f"-- DELETE FROM {first_table} WHERE <condition>;"
    if "update" in q or "change" in q or "modify" in q:
        return f"UPDATE {first_table}\nSET <column> = <value>\nWHERE <condition>;"
    if "insert" in q or "add" in q or "create" in q or "new" in q:
        if tables:
            t = next((t for t in schema["tables"] if t["name"] == first_table), None)
            if t and t.get("columns"):
                non_pk_cols = [c["name"] for c in t["columns"] if not c.get("primary_key")]
                if non_pk_cols:
                    cols_str = ", ".join(non_pk_cols)
                    vals_str = ", ".join([f"<{c}>" for c in non_pk_cols])
                    return f"INSERT INTO {first_table} ({cols_str})\nVALUES ({vals_str});"
        return f"INSERT INTO {first_table} (<columns>) VALUES (<values>);"
    if "join" in q or "with" in q or "related" in q:
        if len(tables) >= 2:
            return (f"SELECT *\nFROM {tables[0]} a\n"
                    f"JOIN {tables[1]} b ON a.<id> = b.<id>\n"
                    f"WHERE <condition>;")
    return f"SELECT * FROM {first_table}\nWHERE <condition>;"


# ══════════════════════════════════════════════════════════════════
# FEATURE 2: Plain-English SQL Explanation via Ollama
# ══════════════════════════════════════════════════════════════════
def explain_sql_with_ollama(sql: str) -> str:
    """Convert SQL into a plain English explanation using local Ollama."""
    system = (
        "You are a helpful database assistant explaining SQL to a non-technical person. "
        "Be clear, friendly, and avoid technical jargon."
    )
    prompt = f"""Given this SQL query:
{sql}

Write a clear explanation in 2-4 sentences that:
- Describes what data will be retrieved or modified
- Mentions any filters or conditions in plain English
- Avoids technical jargon
- Starts with "This query..."

Return ONLY the explanation text."""
    try:
        return _call_ollama(prompt, system=system)
    except Exception:
        return explain_sql_fallback(sql)


def explain_sql_fallback(sql: str) -> str:
    """Basic fallback explanation without AI."""
    sql_upper = sql.upper().strip()
    if sql_upper.startswith("SELECT"):
        if "WHERE" in sql_upper:
            return "This query retrieves specific records from the database that match certain conditions."
        elif "JOIN" in sql_upper:
            return "This query retrieves and combines data from multiple related tables."
        elif "COUNT" in sql_upper or "SUM" in sql_upper or "AVG" in sql_upper:
            return "This query calculates summary statistics (totals, averages, or counts) from the database."
        elif "ORDER BY" in sql_upper:
            return "This query retrieves records from the database sorted in a specific order."
        elif "GROUP BY" in sql_upper:
            return "This query retrieves records grouped by a specific category with aggregated values."
        else:
            return "This query retrieves all records from the specified table."
    elif sql_upper.startswith("INSERT"):
        return "This query adds new records to the database table."
    elif sql_upper.startswith("UPDATE"):
        return "This query modifies existing records in the database that match certain conditions."
    elif sql_upper.startswith("DELETE"):
        return "This query removes specific records from the database that match certain conditions."
    return "This query performs a database operation based on the specified conditions."


# ══════════════════════════════════════════════════════════════════
# FEATURE 4: SQL Accuracy Self-Evaluation
# ══════════════════════════════════════════════════════════════════
def evaluate_sql_accuracy(sql: str, natural_query: str, schema: dict, api_key: str = None) -> dict:
    """
    Evaluate the SQL query for potential issues.
    Returns: { score: 0-100, grade: str, issues: [], suggestions: [], is_valid: bool }
    """
    issues = []
    suggestions = []
    score = 100

    # ── Static analysis checks ──
    sql_stripped = sql.strip()
    sql_upper = sql_stripped.upper()

    # Check for placeholder text
    if "<condition>" in sql or "<columns>" in sql or "<values>" in sql or "<column>" in sql:
        issues.append("Query contains unresolved placeholders — needs specific values.")
        score -= 35

    # Check for dangerous unbounded DELETE/UPDATE
    if sql_upper.startswith("DELETE") and "WHERE" not in sql_upper:
        issues.append("DELETE without WHERE clause will remove ALL records — potentially dangerous.")
        score -= 40

    if sql_upper.startswith("UPDATE") and "WHERE" not in sql_upper:
        issues.append("UPDATE without WHERE clause will modify ALL records.")
        score -= 30

    # Check SELECT * usage
    if "SELECT *" in sql_upper and schema.get("tables"):
        suggestions.append("Consider specifying column names instead of SELECT * for better performance.")
        score -= 5

    # Check for missing LIMIT on large selects
    if sql_upper.startswith("SELECT") and "LIMIT" not in sql_upper and "COUNT" not in sql_upper:
        suggestions.append("Consider adding a LIMIT clause to prevent retrieving too many records.")
        score -= 5

    # Check table names exist in schema
    if schema.get("tables"):
        table_names = [t["name"].lower() for t in schema["tables"]]
        # Extract table names from SQL using regex
        from_tables = re.findall(r'\bFROM\s+(\w+)', sql_upper)
        join_tables = re.findall(r'\bJOIN\s+(\w+)', sql_upper)
        all_refs = from_tables + join_tables
        for ref in all_refs:
            if ref.lower() not in table_names:
                issues.append(f"Table '{ref}' referenced in query may not exist in the detected schema.")
                score -= 20

    # Syntax sanity check: balanced parentheses
    open_count = sql.count("(")
    close_count = sql.count(")")
    if open_count != close_count:
        issues.append(f"Unbalanced parentheses detected ({open_count} opening, {close_count} closing).")
        score -= 25

    # Check for semicolon
    if not sql_stripped.endswith(";"):
        suggestions.append("Query is missing a terminating semicolon.")
        score -= 2

    score = max(0, min(100, score))

    # ── AI-powered evaluation if key available ──
    if api_key and score > 30:
        try:
            ai_eval = _ai_evaluate_sql(sql, natural_query, schema, api_key)
            if ai_eval.get("additional_issues"):
                issues.extend(ai_eval["additional_issues"])
            if ai_eval.get("suggestions"):
                suggestions.extend(ai_eval["suggestions"])
            # Blend scores: 60% static, 40% AI
            ai_score = ai_eval.get("confidence", score)
            score = int(score * 0.6 + ai_score * 0.4)
        except Exception:
            pass

    score = max(0, min(100, score))

    if score >= 85:
        grade = "Excellent"
    elif score >= 70:
        grade = "Good"
    elif score >= 50:
        grade = "Fair"
    else:
        grade = "Needs Review"

    return {
        "score": score,
        "grade": grade,
        "issues": issues,
        "suggestions": suggestions,
        "is_valid": len(issues) == 0
    }


def _ai_evaluate_sql(sql: str, natural_query: str, schema: dict, api_key: str = None) -> dict:
    schema_desc = _build_schema_description(schema)
    system = "You are a senior database developer reviewing generated SQL. Return ONLY valid JSON."
    prompt = f"""{schema_desc}

User's original request: "{natural_query}"

Generated SQL:
{sql}

Evaluate this SQL query for correctness. Return ONLY a JSON object with this structure:
{{
  "confidence": <integer 0-100>,
  "additional_issues": [<list of specific problems found, if any>],
  "suggestions": [<list of improvement suggestions, if any>]
}}

Be concise. If the query looks correct, return empty arrays.
Return ONLY the JSON, no markdown."""
    try:
        raw = _call_ollama(prompt, system=system)
        raw = _clean_json_response(raw)
        return json.loads(raw)
    except Exception:
        return {"confidence": 70, "additional_issues": [], "suggestions": []}


# ══════════════════════════════════════════════════════════════════
# FEATURE 3: Conversational Query Refinement
# ══════════════════════════════════════════════════════════════════
def refine_sql_with_conversation(
    original_sql: str,
    refinement_request: str,
    conversation_history: list,
    schema: dict,
    api_key: str = None
) -> str:
    """Refine an existing SQL query based on follow-up instructions using Ollama."""
    schema_desc = _build_schema_description(schema)
    history_str = ""
    for turn in conversation_history[-6:]:
        role = "User" if turn["role"] == "user" else "System"
        history_str += f"{role}: {turn['content']}\n"

    system = (
        "You are an intelligent SQL assistant. "
        "Return ONLY the updated SQL query — no markdown, no backticks, no explanation."
    )
    prompt = f"""{schema_desc}

Conversation history:
{history_str}

Current SQL query:
{original_sql}

User's refinement request: "{refinement_request}"

Modify the SQL query to incorporate the user's refinement.
Keep all previous conditions unless the user explicitly asks to remove them.
Return ONLY the updated SQL query."""
    raw = _call_ollama(prompt, system=system)
    raw = re.sub(r'^```(?:sql)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```\s*$', '', raw, flags=re.MULTILINE)
    return raw.strip()


# ══════════════════════════════════════════════════════════════════
# AGREEMENT ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/agreement")
def agreement_page():
    """Show Terms & Privacy agreement screen. Public — accessible before login."""
    return render_template("agreement.html")


@app.route("/agreement/accept", methods=["POST"])
@login_required
def accept_agreement():
    """Record that the user has accepted the agreement."""
    current_user.agreement_accepted    = True
    current_user.agreement_accepted_at = datetime.datetime.utcnow()
    db.session.commit()
    next_url = request.form.get("next") or url_for("dashboard")
    return redirect(next_url)



# ══════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("landing"))


@app.route("/home")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


def _validate_username(name: str) -> str | None:
    """Return an error message if name is invalid, else None."""
    if not name:
        return "Full name is required."
    
    # Allows both uppercase/lowercase letters and spaces
    if not re.fullmatch(r"[A-Za-z ]+", name):
        return ("Name must contain letters and spaces only — no numbers, "
                "special characters, or symbols (e.g. John Doe).")
                
    return None


def _validate_password(password: str) -> str | None:
    """Return an error message if password is invalid, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter."
    if not re.search(r"\d", password):
        return "Password must contain at least one number."
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?`~]", password):
        return "Password must contain at least one special character (e.g. !, @, #)."
    return None


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name      = request.form.get("name", "").strip()
        email     = request.form.get("email", "").strip().lower()
        password  = request.form.get("password", "")
        confirm   = request.form.get("confirm_password", "")
        agreement = request.form.get("agreement_accepted", "")

        # 1. Agreement must be accepted before account is created
        if not agreement:
            flash("You must accept the Terms of Use and User Agreement to register.", "error")
            return redirect(url_for("signup"))

        # 2. Username validation — letters only
        name_error = _validate_username(name)
        if name_error:
            flash(name_error, "error")
            return redirect(url_for("signup"))

        # 3. Password strength validation
        password_error = _validate_password(password)
        if password_error:
            flash(password_error, "error")
            return redirect(url_for("signup"))

        # 4. Confirm password
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        # ── Supabase Auth path ─────────────────────────────────────────────
        if supabase:
            logger.info(f"[signup] Using SUPABASE auth path for {email}")

            # Only block signup if a local row is ACTUALLY linked to a real
            # Supabase account. A local row with no supabase_uid is an
            # orphan (stale/failed signup, or pre-Supabase test data) — it
            # has no real account, so we clean it up and let Supabase be
            # the actual source of truth for "is this email taken".
            existing = User.query.filter_by(email=email).first()
            if existing and existing.supabase_uid:
                flash("Email already registered.", "error")
                return redirect(url_for("signup"))
            elif existing and not existing.supabase_uid:
                logger.warning(
                    f"[signup] Removing orphaned local-only row for {email} "
                    f"(no supabase_uid) before retrying signup"
                )
                db.session.delete(existing)
                db.session.commit()

            try:
                res = supabase.auth.sign_up({
                    "email": email,
                    "password": password,
                    "options": {"data": {"name": name}},
                })
                sb_user = res.user
                if not sb_user:
                    flash("Registration failed. Please try again.", "error")
                    return redirect(url_for("signup"))

                now  = datetime.datetime.utcnow()
                user = User(
                    supabase_uid=sb_user.id,
                    name=name,
                    email=email,
                    agreement_accepted=True,
                    agreement_accepted_at=now,
                )
                db.session.add(user)
                db.session.commit()
                login_user(user, remember=False)
                flash("Welcome to SchemaLens! Check your email to confirm your account.", "success")
                return redirect(url_for("dashboard"))
            except Exception as e:
                logger.error(f"Supabase signup error: {e}")
                # Surface a readable message if Supabase returns one
                msg = str(e)
                if "already registered" in msg.lower() or "already exists" in msg.lower():
                    flash("Email already registered.", "error")
                else:
                    flash(f"Registration error: {msg}", "error")
                return redirect(url_for("signup"))

        # ── Fallback: local auth (no Supabase configured) ──────────────────
        logger.warning(
            f"[signup] Using LOCAL SQLITE fallback for {email} "
            f"(supabase client inactive — reason: {_supabase_init_error})"
        )
        from werkzeug.security import generate_password_hash
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for("signup"))

        now  = datetime.datetime.utcnow()
        user = User(
            name=name,
            email=email,
            password=generate_password_hash(password, method="scrypt"),
            agreement_accepted=True,
            agreement_accepted_at=now,
        )
        db.session.add(user)
        db.session.commit()
        login_user(user, remember=False)
        flash("Welcome to SchemaLens! Your account has been created.", "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/debug/auth-status")
def debug_auth_status():
    """Diagnostic endpoint — check whether Supabase auth is actually active."""
    return jsonify({
        "supabase_available_package": _SUPABASE_AVAILABLE,
        "supabase_url_set": bool(SUPABASE_URL),
        "supabase_url_value": SUPABASE_URL if SUPABASE_URL else None,
        "supabase_anon_key_set": bool(SUPABASE_ANON_KEY),
        "supabase_client_active": supabase is not None,
        "init_error": _supabase_init_error,
        "auth_mode": "SUPABASE" if supabase else "LOCAL_SQLITE_FALLBACK",
    })


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        # ── Supabase Auth path ─────────────────────────────────────────────
        if supabase:
            try:
                res     = supabase.auth.sign_in_with_password({"email": email, "password": password})
                sb_user = res.user
                if not sb_user:
                    flash("Invalid email or password.", "error")
                    return render_template("login.html")

                # Find local user by supabase_uid, fall back to email (migration)
                user = User.query.filter_by(supabase_uid=sb_user.id).first()
                if not user:
                    user = User.query.filter_by(email=email).first()
                    if user:
                        # Migrate existing local account → link supabase_uid
                        user.supabase_uid = sb_user.id
                        db.session.commit()
                    else:
                        # No local row at all — create one from Supabase data.
                        # Supabase is the real source of truth for "does this
                        # user exist"; the local row only stores app-specific
                        # data (schemas, queries, badges, etc.).
                        sb_metadata = getattr(sb_user, "user_metadata", None) or {}
                        user = User(
                            supabase_uid=sb_user.id,
                            name=sb_metadata.get("name") or email.split("@")[0],
                            email=sb_user.email or email,
                            agreement_accepted=True,
                            agreement_accepted_at=datetime.datetime.utcnow(),
                        )
                        db.session.add(user)
                        db.session.commit()

                # Supabase is the source of truth for name/email — sync them in
                user = _sync_user_from_supabase(user, sb_user)

                login_user(user, remember=remember)
                return redirect(request.args.get("next") or url_for("dashboard"))
            except Exception as e:
                logger.error(f"Supabase login error: {e}")
                flash("Invalid email or password.", "error")
            return render_template("login.html")

        # ── Fallback: local auth (no Supabase configured) ──────────────────
        from werkzeug.security import check_password_hash
        user = User.query.filter_by(email=email).first()
        if user and user.password and check_password_hash(user.password, password):
            login_user(user, remember=remember)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    if supabase:
        try:
            supabase.auth.sign_out()
        except Exception:
            pass
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


# ══════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════
@app.route("/dashboard")
@login_required
def dashboard():


    #Agreement gate: force users to accept before accessing dashboard
    if not current_user.agreement_accepted:
        return redirect(url_for("agreement_page", next=url_for("dashboard")))
    schemas  = SchemaUpload.query.filter_by(user_id=current_user.id)\
                .order_by(SchemaUpload.uploaded_at.desc()).all()
    recent_q = QueryHistory.query.filter_by(user_id=current_user.id)\
                .order_by(QueryHistory.created_at.desc()).limit(5).all()
    total_q  = QueryHistory.query.filter_by(user_id=current_user.id).count()
    return render_template("dashboard.html",
                           schemas=schemas, recent_queries=recent_q,
                           total_queries=total_q)


# ══════════════════════════════════════════════════════════════════
# SCHEMA UPLOAD & MANAGEMENT
# ══════════════════════════════════════════════════════════════════
@app.route("/schemas")
@login_required
def schemas():
    all_schemas = SchemaUpload.query.filter_by(user_id=current_user.id)\
                  .order_by(SchemaUpload.uploaded_at.desc()).all()
    return render_template("schemas.html", schemas=all_schemas)


@app.route("/schemas/upload", methods=["POST"])
@login_required
def upload_schema():
    if "schema_image" not in request.files:
        flash("No file selected.", "error")
        return redirect(url_for("schemas"))
    file = request.files["schema_image"]
    schema_name = request.form.get("schema_name", "").strip() or file.filename
    if file.filename == "" or not allowed_file(file.filename):
        flash("Invalid file. Please upload a PNG, JPG, or similar image.", "error")
        return redirect(url_for("schemas"))
    filename = secure_filename(
        f"user{current_user.id}_{int(datetime.datetime.utcnow().timestamp())}_{file.filename}"
    )
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    api_key = _get_gemini_key()

    # ── PARALLEL #1: OCR + Gemini Vision fire simultaneously ──────────────
    # Both are I/O-bound so they overlap cleanly; saves ~2-5s per upload.
    ocr_text = ""
    parsed   = {}
    if api_key:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_ocr    = pool.submit(run_ocr, filepath)
            f_vision = pool.submit(parse_schema_with_image, filepath, api_key)
            ocr_text = f_ocr.result()
            try:
                vision_result = f_vision.result()
                if vision_result.get("tables"):
                    parsed = vision_result          # Vision succeeded — use it
            except Exception:
                parsed = {}                         # Vision failed — fall through

        # If Vision didn't yield tables, run Gemini + Ollama in parallel (consensus)
        if not parsed.get("tables"):
            try:
                parsed = parse_schema_with_consensus(ocr_text, api_key, image_path=None)
            except Exception as exc2:
                parsed = parse_schema_fallback(ocr_text)
                flash(
                    f"AI parsing encountered issues ({exc2}); used heuristic parser. "
                    "Review the schema on the next page and correct any errors.",
                    "warning"
                )
    else:
        ocr_text = run_ocr(filepath)
        parsed   = parse_schema_fallback(ocr_text)
        if parsed.get("tables"):
            flash(
                "AI-powered parsing is currently unavailable — used the heuristic "
                "parser instead. Review the detected tables for accuracy.",
                "info"
            )
        else:
            flash(
                "The heuristic parser found no tables in this image. "
                "Please add tables manually on the confirmation page.",
                "warning"
            )
    schema = SchemaUpload(
        user_id=current_user.id,
        filename=filename,
        original_name=file.filename,
        schema_name=schema_name,
        ocr_text=ocr_text,
        parsed_schema=json.dumps(parsed),
        confirmed_schema=""
    )
    db.session.add(schema)
    db.session.commit()
    check_and_award_badges(current_user.id)
    flash("Schema uploaded! Please review and confirm the detected schema below.", "success")
    # Redirect to confirmation page (Feature 1)
    return redirect(url_for("schema_confirm", schema_id=schema.id))


# ── FEATURE 1: Schema Confirmation Page ──
@app.route("/schemas/<int:schema_id>/confirm", methods=["GET"])
@login_required
def schema_confirm(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    parsed = json.loads(schema.parsed_schema) if schema.parsed_schema else {"tables": [], "relationships": []}
    return render_template("schema_confirm.html", schema=schema, parsed=parsed)


@app.route("/schemas/<int:schema_id>/confirm", methods=["POST"])
@login_required
def schema_confirm_save(schema_id):
    """Save user-confirmed/edited schema."""
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    confirmed_data = request.get_json()
    if not confirmed_data:
        return jsonify({"error": "No data provided"}), 400
    schema.confirmed_schema = json.dumps(confirmed_data)
    db.session.commit()
    return jsonify({"ok": True, "redirect": url_for("schema_detail", schema_id=schema_id)})


@app.route("/schemas/<int:schema_id>")
@login_required
def schema_detail(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    # Use confirmed schema if available, else fall back to parsed
    if schema.confirmed_schema:
        parsed = json.loads(schema.confirmed_schema)
    elif schema.parsed_schema:
        parsed = json.loads(schema.parsed_schema)
    else:
        parsed = {}
    queries = QueryHistory.query.filter_by(schema_id=schema_id)\
              .order_by(QueryHistory.created_at.desc()).all()
    return render_template("schema_detail.html", schema=schema, parsed=parsed, queries=queries)


@app.route("/schemas/<int:schema_id>/delete", methods=["POST"])
@login_required
def delete_schema(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    try:
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], schema.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass
    db.session.delete(schema)
    db.session.commit()
    flash("Schema deleted.", "success")
    return redirect(url_for("schemas"))


# ── FEATURE 5: ER Diagram data endpoint ──
@app.route("/schemas/<int:schema_id>/er-diagram")
@login_required
def er_diagram(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    if schema.confirmed_schema:
        parsed = json.loads(schema.confirmed_schema)
    elif schema.parsed_schema:
        parsed = json.loads(schema.parsed_schema)
    else:
        parsed = {"tables": [], "relationships": []}
    return render_template("er_diagram.html", schema=schema, parsed_json=json.dumps(parsed))


# ══════════════════════════════════════════════════════════════════
# QUERY GENERATION (enhanced with Features 2, 3, 4)
# ══════════════════════════════════════════════════════════════════
@app.route("/query", methods=["GET"])
@login_required
def query_page():
    schemas = SchemaUpload.query.filter_by(user_id=current_user.id)\
              .order_by(SchemaUpload.uploaded_at.desc()).all()
    selected_schema = None
    preselect_id = request.args.get("schema_id")
    if preselect_id:
        selected_schema = SchemaUpload.query.filter_by(
            id=preselect_id, user_id=current_user.id).first()
    return render_template("query.html", schemas=schemas, selected_schema=selected_schema)


@app.route("/api/generate-sql", methods=["POST"])
@login_required
def generate_sql():
    """
    Enhanced SQL generation with:
    - Feature 2: SQL explanation
    - Feature 4: accuracy evaluation
    - Feature 3: session context stored for refinement
    """
    data = request.get_json()
    natural_query = (data.get("query") or "").strip()
    schema_id     = data.get("schema_id")

    if not natural_query:
        return jsonify({"error": "Query cannot be empty."}), 400

    schema_obj = None
    parsed = {}
    if schema_id:
        schema_obj = SchemaUpload.query.filter_by(
            id=schema_id, user_id=current_user.id).first()
        if schema_obj:
            # Prefer confirmed schema
            raw = schema_obj.confirmed_schema or schema_obj.parsed_schema
            if raw:
                parsed = json.loads(raw)

    sql = ""
    model_used = ""

    ollama_ok = check_ollama_active()

    if ollama_ok and parsed.get("tables"):
        try:
            sql = generate_sql_with_ollama(natural_query, parsed)
            model_used = f"ollama/{OLLAMA_MODEL}"
        except Exception as exc:
            # Ollama failed — fall back to heuristic
            sql = _fallback_sql(natural_query, parsed)
            model_used = "heuristic"
    else:
        sql = _fallback_sql(natural_query, parsed)
        model_used = "heuristic"

    # ── PARALLEL #2: Explain + Evaluate fire simultaneously (fork-join) ──────
    # Neither depends on the other — pure parallelism, ~2x faster response.
    def _explain():
        if ollama_ok:
            try:
                return explain_sql_with_ollama(sql)
            except Exception:
                return explain_sql_fallback(sql)
        return explain_sql_fallback(sql)

    def _evaluate():
        return evaluate_sql_accuracy(sql, natural_query, parsed, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_explain  = pool.submit(_explain)
        f_evaluate = pool.submit(_evaluate)
        explanation = f_explain.result()
        eval_result = f_evaluate.result()

    # Feature 3: Initialize conversation context
    conversation = [
        {"role": "user", "content": natural_query},
        {"role": "assistant", "content": sql}
    ]

    history = QueryHistory(
        user_id=current_user.id,
        schema_id=schema_obj.id if schema_obj else None,
        natural_query=natural_query,
        generated_sql=sql,
        model_used=model_used,
        sql_explanation=explanation,
        accuracy_score=eval_result["score"],
        accuracy_notes=json.dumps(eval_result),
        conversation_context=json.dumps(conversation)
    )
    db.session.add(history)
    db.session.commit()

    # Award any newly earned badges (first_query, query_10, query_50 …)
    newly_awarded = check_and_award_badges(current_user.id)

    return jsonify({
        "sql": sql,
        "model": model_used,
        "query_id": history.id,
        "explanation": explanation,
        "evaluation": eval_result,
        "ollama_active": ollama_ok,
        "new_badges": newly_awarded,
    })


# ── FEATURE 3: Conversational Refinement endpoint ──
@app.route("/api/refine-sql", methods=["POST"])
@login_required
def refine_sql():
    """
    Refine an existing SQL query through follow-up natural language instructions.
    Maintains full conversation context.
    """
    data = request.get_json()
    query_id         = data.get("query_id")
    refinement       = (data.get("refinement") or "").strip()
    schema_id        = data.get("schema_id")

    if not refinement:
        return jsonify({"error": "Refinement instruction cannot be empty."}), 400

    # Load existing query
    history_obj = None
    if query_id:
        history_obj = QueryHistory.query.filter_by(
            id=query_id, user_id=current_user.id).first()

    if not history_obj:
        return jsonify({"error": "Original query not found."}), 404

    # Load schema
    parsed = {}
    schema_obj = None
    if schema_id:
        schema_obj = SchemaUpload.query.filter_by(
            id=schema_id, user_id=current_user.id).first()
        if schema_obj:
            raw = schema_obj.confirmed_schema or schema_obj.parsed_schema
            if raw:
                parsed = json.loads(raw)

    if not check_ollama_active():
        return jsonify({"error": "Ollama is not running. Please start Ollama to use query refinement."}), 400

    # Load conversation history
    try:
        conversation = json.loads(history_obj.conversation_context or "[]")
    except Exception:
        conversation = []

    # Add refinement to conversation
    conversation.append({"role": "user", "content": refinement})

    try:
        new_sql = refine_sql_with_conversation(
            history_obj.generated_sql,
            refinement,
            conversation,
            parsed
        )
    except Exception as exc:
        return jsonify({"error": f"Refinement failed: {exc}"}), 500

    # Update conversation
    conversation.append({"role": "assistant", "content": new_sql})

    # Feature 2: New explanation via Ollama
    try:
        explanation = explain_sql_with_ollama(new_sql)
    except Exception:
        explanation = explain_sql_fallback(new_sql)

    # Feature 4: Re-evaluate
    eval_result = evaluate_sql_accuracy(new_sql, refinement, parsed, None)

    # Save refined query as new history entry
    new_history = QueryHistory(
        user_id=current_user.id,
        schema_id=schema_obj.id if schema_obj else history_obj.schema_id,
        natural_query=f"[Refined] {refinement}",
        generated_sql=new_sql,
        model_used=f"ollama/{OLLAMA_MODEL}",
        sql_explanation=explanation,
        accuracy_score=eval_result["score"],
        accuracy_notes=json.dumps(eval_result),
        conversation_context=json.dumps(conversation)
    )
    db.session.add(new_history)
    db.session.commit()

    return jsonify({
        "sql": new_sql,
        "query_id": new_history.id,
        "explanation": explanation,
        "evaluation": eval_result,
        "conversation": conversation
    })


# ── FEATURE 2: Standalone explanation endpoint ──
@app.route("/api/explain-sql", methods=["POST"])
@login_required
def explain_sql_endpoint():
    data = request.get_json()
    sql = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "SQL cannot be empty."}), 400
    if check_ollama_active():
        try:
            explanation = explain_sql_with_ollama(sql)
        except Exception:
            explanation = explain_sql_fallback(sql)
    else:
        explanation = explain_sql_fallback(sql)
    return jsonify({"explanation": explanation})


# ── Schema metadata endpoint for ERD overview card ──
@app.route("/api/schema/<int:schema_id>/metadata")
@login_required
def schema_metadata(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first()
    if not schema:
        return jsonify({"error": "Schema not found"}), 404
    raw = schema.confirmed_schema or schema.parsed_schema or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"tables": [], "relationships": []}
    else:
        parsed = {"tables": [], "relationships": []}
    return jsonify({
        "id": schema.id,
        "name": schema.schema_name or schema.original_name,
        "confirmed": bool(schema.confirmed_schema),
        "tables": parsed.get("tables", []),
        "relationships": parsed.get("relationships", []),
    })


# ── Ollama status endpoint ──
@app.route("/api/ollama-status")
@login_required
def ollama_status():
    active = check_ollama_active()
    return jsonify({"active": active, "model": OLLAMA_MODEL, "url": OLLAMA_BASE_URL})


# ── FEATURE 4: Standalone evaluation endpoint ──
@app.route("/api/evaluate-sql", methods=["POST"])
@login_required
def evaluate_sql_endpoint():
    data = request.get_json()
    sql           = (data.get("sql") or "").strip()
    natural_query = (data.get("query") or "").strip()
    schema_id     = data.get("schema_id")
    if not sql:
        return jsonify({"error": "SQL cannot be empty."}), 400
    parsed = {}
    if schema_id:
        schema_obj = SchemaUpload.query.filter_by(
            id=schema_id, user_id=current_user.id).first()
        if schema_obj:
            raw = schema_obj.confirmed_schema or schema_obj.parsed_schema
            if raw:
                parsed = json.loads(raw)
    result = evaluate_sql_accuracy(sql, natural_query, parsed, None)
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════
# QUERY HISTORY
# ══════════════════════════════════════════════════════════════════
@app.route("/history")
@login_required
def history():
    page = request.args.get("page", 1, type=int)
    per_page = 20
    queries = QueryHistory.query.filter_by(user_id=current_user.id)\
              .order_by(QueryHistory.created_at.desc())\
              .paginate(page=page, per_page=per_page, error_out=False)
    return render_template("history.html", queries=queries)


@app.route("/history/<int:query_id>/delete", methods=["POST"])
@login_required
def delete_query(query_id):
    q = QueryHistory.query.filter_by(id=query_id, user_id=current_user.id).first_or_404()
    db.session.delete(q)
    db.session.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_profile":
            name    = request.form.get("name", "").strip()
            email   = request.form.get("email", "").strip().lower()
            company = request.form.get("company", "").strip()
            role    = request.form.get("role", "").strip()
            existing = User.query.filter_by(email=email).first()
            if existing and existing.id != current_user.id:
                flash("That email is already taken.", "error")
            else:
                current_user.name    = name
                current_user.email   = email
                current_user.company = company
                current_user.role    = role
                db.session.commit()
                flash("Profile updated successfully!", "success")
        elif action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw     = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if new_pw != confirm_pw:
                flash("New passwords do not match.", "error")
            elif len(new_pw) < 6:
                flash("Password must be at least 6 characters.", "error")
            else:
                if supabase:
                    # Re-authenticate to verify current password, then update
                    try:
                        supabase.auth.sign_in_with_password({
                            "email": current_user.email,
                            "password": current_pw,
                        })
                        supabase.auth.update_user({"password": new_pw})
                        flash("Password changed successfully!", "success")
                    except Exception as e:
                        logger.error(f"Supabase password change error: {e}")
                        flash("Current password is incorrect.", "error")
                else:
                    from werkzeug.security import check_password_hash, generate_password_hash
                    if not current_user.password or not check_password_hash(current_user.password, current_pw):
                        flash("Current password is incorrect.", "error")
                    else:
                        current_user.password = generate_password_hash(new_pw, method="scrypt")
                        db.session.commit()
                        flash("Password changed successfully!", "success")
        elif action == "delete_account":
            db.session.delete(current_user)
            db.session.commit()
            logout_user()
            flash("Account deleted.", "success")
            return redirect(url_for("signup"))
        return redirect(url_for("settings"))
    return render_template("settings.html", user=current_user)


# ══════════════════════════════════════════════════════════════════
# BADGE AWARDING HELPER
# ══════════════════════════════════════════════════════════════════

def award_badge_if_new(user_id: int, badge_key: str) -> dict | None:
    """
    Award a badge to the user if they don't already have it.
    Returns the badge dict if newly awarded, else None.
    """
    if badge_key not in BADGES:
        return None
    existing = UserBadge.query.filter_by(user_id=user_id, badge_key=badge_key).first()
    if existing:
        return None
    badge = UserBadge(user_id=user_id, badge_key=badge_key)
    db.session.add(badge)
    db.session.commit()
    return BADGES[badge_key]


def check_and_award_badges(user_id: int) -> list[dict]:
    """
    Check all badge conditions for a user and award any newly earned badges.
    Returns list of newly awarded badge dicts (for flash/toast notifications).
    """
    newly_awarded = []

    schema_count = SchemaUpload.query.filter_by(user_id=user_id).count()
    query_count  = QueryHistory.query.filter_by(user_id=user_id).count()

    thresholds = [
        ("first_upload",   schema_count >= 1),
        ("first_query",    query_count  >= 1),
        ("query_10",       query_count  >= 10),
        ("query_50",       query_count  >= 50),
        ("schema_5",       schema_count >= 5),
    ]

    for key, condition in thresholds:
        if condition:
            result = award_badge_if_new(user_id, key)
            if result:
                newly_awarded.append({**result, "key": key})

    return newly_awarded


def update_streak(user: "User") -> int:
    """
    Update the user's login streak.
    Returns the current streak count.
    """
    now = datetime.datetime.utcnow()
    last = user.last_active

    if last is None:
        user.streak_days = 1
    else:
        diff = (now.date() - last.date()).days
        if diff == 0:
            pass                    # same day — no change
        elif diff == 1:
            user.streak_days = (user.streak_days or 0) + 1
        else:
            user.streak_days = 1   # streak broken

    user.last_active = now
    db.session.commit()

    if user.streak_days >= 3:
        award_badge_if_new(user.id, "streak_3")

    return user.streak_days

# BADGE DEFINITIONS
# ══════════════════════════════════════════════════════════════════

BADGES = {
    "first_upload": {
        "label": "First Upload",
        "description": "Uploaded your first ER diagram schema.",
        "icon": "📊",
        "color": "#4e9af1",
    },
    "first_query": {
        "label": "Query Starter",
        "description": "Generated your first SQL query.",
        "icon": "🔍",
        "color": "#34c87a",
    },
    "query_10": {
        "label": "Power User",
        "description": "Generated 10 or more SQL queries.",
        "icon": "🚀",
        "color": "#f5a623",
    },
    "query_50": {
        "label": "SQL Veteran",
        "description": "Generated 50 or more SQL queries.",
        "icon": "🏆",
        "color": "#e84393",
    },
    "schema_5": {
        "label": "Schema Expert",
        "description": "Uploaded 5 or more schemas.",
        "icon": "🗄️",
        "color": "#9b59b6",
    },
    "streak_3": {
        "label": "On a Roll",
        "description": "Logged in 3 days in a row.",
        "icon": "🔥",
        "color": "#e74c3c",
    },
    "challenge_first": {
        "label": "Challenger",
        "description": "Completed your first SQL Challenge.",
        "icon": "⚔️",
        "color": "#1abc9c",
    },
    "challenge_perfect": {
        "label": "Perfectionist",
        "description": "Scored 90+ on a SQL Challenge.",
        "icon": "💎",
        "color": "#3498db",
    },
}


# ══════════════════════════════════════════════════════════════════
# GAMIFICATION ROUTES — paste into app.py
# ══════════════════════════════════════════════════════════════════

# ── Badges page ───────────────────────────────────────────────────
@app.route("/badges")
@login_required
def badges_page():
    # Auto-award any badges the user has earned but not yet been granted
    check_and_award_badges(current_user.id)

    user_badge_rows = UserBadge.query.filter_by(user_id=current_user.id).all()
    earned      = {b.badge_key for b in user_badge_rows}
    badge_dates = {b.badge_key: b.awarded_at for b in user_badge_rows}

    schema_count    = SchemaUpload.query.filter_by(user_id=current_user.id).count()
    query_count     = QueryHistory.query.filter_by(user_id=current_user.id).count()
    streak          = getattr(current_user, 'streak_days', 0) or 0

    all_attempts    = ChallengeAttempt.query.filter_by(user_id=current_user.id).all()
    challenge_count = len(all_attempts)
    scores          = [a.score for a in all_attempts if a.score is not None]
    best_score      = max(scores) if scores else 0
    perfect_count   = sum(1 for s in scores if s >= 90)

    xp          = (len(earned) * 100) + (query_count * 5) + (schema_count * 20) + (challenge_count * 15) + (streak * 10)
    level       = max(1, xp // 200 + 1)
    xp_in_level = xp % 200
    xp_to_next  = 200

    badge_progress = {
        "first_upload":      {"current": schema_count,    "target": 1},
        "first_query":       {"current": query_count,     "target": 1},
        "query_10":          {"current": query_count,     "target": 10},
        "query_50":          {"current": query_count,     "target": 50},
        "schema_5":          {"current": schema_count,    "target": 5},
        "streak_3":          {"current": streak,          "target": 3},
        "challenge_first":   {"current": challenge_count, "target": 1},
        "challenge_perfect": {"current": perfect_count,   "target": 1},
    }

    badge_list = []
    for key, info in BADGES.items():
        prog = badge_progress.get(key, {"current": 0, "target": 1})
        pct  = min(100, int((prog["current"] / prog["target"]) * 100)) if prog["target"] else 0
        badge_list.append({**info, "key": key, "earned": key in earned,
                           "progress": pct, "current": prog["current"], "target": prog["target"]})

    locked     = [b for b in badge_list if not b["earned"]]
    next_badge = max(locked, key=lambda b: b["progress"]) if locked else None

    return render_template(
        "badges.html",
        badges=badge_list,
        earned_count=len(earned),
        total_count=len(BADGES),
        schema_count=schema_count,
        query_count=query_count,
        streak=streak,
        challenge_count=challenge_count,
        best_score=best_score,
        perfect_count=perfect_count,
        xp=xp,
        level=level,
        xp_in_level=xp_in_level,
        xp_to_next=xp_to_next,
        next_badge=next_badge,
        badge_dates=badge_dates,
    )

# ── Leaderboard ───────────────────────────────────────────────────
@app.route("/leaderboard")
@login_required
def leaderboard():
    from sqlalchemy import func

    # ── Aggregate counts per user via LEFT OUTER JOIN so ALL users appear ──
    all_users = User.query.all()

    def user_stats(u):
        qc = QueryHistory.query.filter_by(user_id=u.id).count()
        sc = SchemaUpload.query.filter_by(user_id=u.id).count()
        bc = UserBadge.query.filter_by(user_id=u.id).count()
        attempts = ChallengeAttempt.query.filter_by(user_id=u.id).all()
        cc = len(attempts)
        scores = [a.score for a in attempts if a.score is not None]
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0
        best_score = max(scores) if scores else 0
        streak = getattr(u, 'streak_days', 0) or 0
        xp = (bc * 100) + (qc * 5) + (sc * 20) + (cc * 15) + (streak * 10)
        level = max(1, xp // 200 + 1)
        return {
            "id": u.id, "name": u.name,
            "query_count": qc, "schema_count": sc,
            "badge_count": bc, "challenge_count": cc,
            "avg_score": avg_score, "best_score": best_score,
            "streak": streak, "xp": xp, "level": level,
        }

    all_stats = [user_stats(u) for u in all_users]

    # Sort each leaderboard — include ALL users with >0 activity
    top_xp      = sorted(all_stats, key=lambda r: r["xp"],          reverse=True)[:10]
    top_queries = sorted([r for r in all_stats if r["query_count"]  > 0], key=lambda r: r["query_count"],  reverse=True)[:10]
    top_schemas = sorted([r for r in all_stats if r["schema_count"] > 0], key=lambda r: r["schema_count"], reverse=True)[:10]
    top_badges  = sorted([r for r in all_stats if r["badge_count"]  > 0], key=lambda r: r["badge_count"],  reverse=True)[:10]
    top_scores  = sorted([r for r in all_stats if r["avg_score"]    > 0], key=lambda r: r["avg_score"],    reverse=True)[:10]

    # Current user stats
    me = next((r for r in all_stats if r["id"] == current_user.id), None)
    if me is None:
        me = user_stats(current_user)

    def my_rank(board):
        for i, r in enumerate(board):
            if r["id"] == current_user.id:
                return i + 1
        # Not in top 10 — find true rank across all
        uid = current_user.id
        if board and "xp" in board[0]:
            full = sorted(all_stats, key=lambda r: r["xp"], reverse=True)
        elif board and "query_count" in board[0]:
            full = sorted(all_stats, key=lambda r: r["query_count"], reverse=True)
        elif board and "schema_count" in board[0]:
            full = sorted(all_stats, key=lambda r: r["schema_count"], reverse=True)
        elif board and "badge_count" in board[0]:
            full = sorted(all_stats, key=lambda r: r["badge_count"], reverse=True)
        else:
            full = sorted(all_stats, key=lambda r: r["avg_score"], reverse=True)
        for i, r in enumerate(full):
            if r["id"] == uid:
                return i + 1
        return None

    return render_template(
        "leaderboard.html",
        top_xp=top_xp,
        top_queries=top_queries,
        top_schemas=top_schemas,
        top_badges=top_badges,
        top_scores=top_scores,
        me=me,
        xp_rank=my_rank(top_xp),
        query_rank=my_rank(top_queries),
        schema_rank=my_rank(top_schemas),
        badge_rank=my_rank(top_badges),
        score_rank=my_rank(top_scores),
    )


class UserBadge(db.Model):
    __tablename__ = "user_badges"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    badge_key   = db.Column(db.String(80), nullable=False)
    awarded_at  = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "badge_key"),)

class ChallengeAttempt(db.Model):
    __tablename__ = "challenge_attempts"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    schema_id     = db.Column(db.Integer, db.ForeignKey("schema_uploads.id"), nullable=True)
    prompt        = db.Column(db.Text, nullable=False)
    expected_sql  = db.Column(db.Text, default="")
    user_sql      = db.Column(db.Text, default="")
    score         = db.Column(db.Integer, default=0)
    feedback      = db.Column(db.Text, default="")
    attempted_at  = db.Column(db.DateTime, default=datetime.datetime.utcnow)


# ── SQL Challenge — GET: pick a schema and show a random prompt ───
@app.route("/challenge")
@login_required
def challenge_page():
    schemas = SchemaUpload.query.filter_by(user_id=current_user.id).all()
    selected_schema = None
    preselect_id = request.args.get("schema_id")
    if preselect_id:
        selected_schema = SchemaUpload.query.filter_by(
            id=preselect_id, user_id=current_user.id).first()

    recent_attempts = (
        ChallengeAttempt.query
        .filter_by(user_id=current_user.id)
        .order_by(ChallengeAttempt.attempted_at.desc())
        .limit(5)
        .all()
    )

    # Accurate stats from DB
    all_attempts = ChallengeAttempt.query.filter_by(user_id=current_user.id).all()
    total_attempts = len(all_attempts)
    scores = [a.score for a in all_attempts if a.score is not None]
    avg_score    = round(sum(scores) / len(scores)) if scores else 0
    best_score   = max(scores) if scores else 0
    perfect_count = sum(1 for s in scores if s >= 90)

    return render_template(
        "challenges.html",
        schemas=schemas,
        selected_schema=selected_schema,
        recent_attempts=recent_attempts,
        total_attempts=total_attempts,
        avg_score=avg_score,
        best_score=best_score,
        perfect_count=perfect_count,
    )


# ── SQL Challenge — API: generate a challenge prompt ─────────────
@app.route("/api/challenge/generate", methods=["POST"])
@login_required
def challenge_generate():
    """
    Given a schema_id, generate a natural-language SQL challenge prompt
    and a reference SQL answer using Gemini (or a fallback prompt bank).
    """
    data       = request.get_json()
    schema_id  = data.get("schema_id")
    difficulty = data.get("difficulty", "easy")
    if not schema_id:
        return jsonify({"error": "Please select a schema first."}), 400

    schema_obj = SchemaUpload.query.filter_by(
        id=schema_id, user_id=current_user.id).first()
    if not schema_obj:
        return jsonify({"error": "Schema not found."}), 404

    raw = schema_obj.confirmed_schema or schema_obj.parsed_schema
    parsed = json.loads(raw) if raw else {}
    tables = parsed.get("tables", [])

    if not tables:
        return jsonify({"error": "Schema has no tables. Please upload and confirm a schema first."}), 400

    api_key = _get_gemini_key()

    if api_key:
        prompt_nl, ref_sql = _generate_challenge_with_gemini(parsed, api_key, difficulty)
    else:
        prompt_nl, ref_sql = _generate_challenge_fallback(parsed, difficulty)

    return jsonify({
        "prompt": prompt_nl,
        "schema_id": schema_id,
        "ref_sql": ref_sql,      # sent to client but hidden from user
    })


# ── SQL Challenge — API: submit and grade the user's answer ───────
@app.route("/api/challenge/submit", methods=["POST"])
@login_required
def challenge_submit():
    data         = request.get_json()
    schema_id    = data.get("schema_id")
    prompt_nl    = (data.get("prompt") or "").strip()
    ref_sql      = (data.get("ref_sql") or "").strip()
    user_sql     = (data.get("user_sql") or "").strip()
    try_number        = int(data.get("try_number", 1))
    timer_penalty     = int(data.get("timer_penalty", 0))
    try_penalty       = int(data.get("try_penalty", 0))
    difficulty        = data.get("difficulty", "easy")

    if not user_sql:
        return jsonify({"error": "Please write a SQL query before submitting."}), 400

    schema_obj = SchemaUpload.query.filter_by(
        id=schema_id, user_id=current_user.id).first() if schema_id else None
    raw = schema_obj.confirmed_schema or schema_obj.parsed_schema if schema_obj else ""
    parsed = json.loads(raw) if raw else {}

    api_key = _get_gemini_key()

    # Grade the raw submission
    if api_key:
        base_score, feedback = _grade_challenge_with_gemini(
            prompt_nl, ref_sql, user_sql, parsed, api_key
        )
        # Also fetch hints for multi-try support
        hints = _generate_hints_with_gemini(prompt_nl, ref_sql, user_sql, parsed, api_key)
    else:
        base_score, feedback = _grade_challenge_fallback(ref_sql, user_sql)
        hints = _generate_hints_fallback(ref_sql, user_sql)

    # Apply penalties (cap total penalty at 25)
    total_penalty    = min(25, timer_penalty + try_penalty)
    difficulty_bonus = 5 if (difficulty == "hard" and base_score >= 70) else 0
    final_score      = max(0, min(100, base_score - total_penalty + difficulty_bonus))

    # Only save to DB on the final try or if passing
    is_final = (base_score >= 85) or (try_number >= 3)
    newly_awarded = []

    if is_final:
        attempt = ChallengeAttempt(
            user_id=current_user.id,
            schema_id=schema_obj.id if schema_obj else None,
            prompt=prompt_nl,
            expected_sql=ref_sql,
            user_sql=user_sql,
            score=final_score,
            feedback=feedback,
        )
        attempt.difficulty = difficulty  # stored if column exists
        db.session.add(attempt)
        db.session.commit()

        result = award_badge_if_new(current_user.id, "challenge_first")
        if result:
            newly_awarded.append({**result, "key": "challenge_first"})
        if final_score >= 90:
            result = award_badge_if_new(current_user.id, "challenge_perfect")
            if result:
                newly_awarded.append({**result, "key": "challenge_perfect"})

    return jsonify({
        "score":             base_score,
        "final_score":       final_score,
        "base_score":        base_score,
        "timer_penalty":     timer_penalty,
        "try_penalty":       try_penalty,
        "difficulty_bonus":  difficulty_bonus,
        "feedback":          feedback,
        "ref_sql":           ref_sql,
        "hints":             hints,
        "new_badges":        newly_awarded,
        "is_final":          is_final,
    })


# ── API: award badges after query/schema actions ──────────────────
@app.route("/api/check-badges", methods=["POST"])
@login_required
def api_check_badges():
    newly = check_and_award_badges(current_user.id)
    return jsonify({"new_badges": newly})


# ══════════════════════════════════════════════════════════════════
# CHALLENGE HELPERS
# ══════════════════════════════════════════════════════════════════

_CHALLENGE_TEMPLATES = [
    ("List all records from {table}.",
     "SELECT * FROM {table};"),
    ("Count the total number of records in {table}.",
     "SELECT COUNT(*) FROM {table};"),
    ("Show the first 5 records from {table} ordered by {col}.",
     "SELECT * FROM {table} ORDER BY {col} LIMIT 5;"),
    ("Find all records in {table} where {col} is not null.",
     "SELECT * FROM {table} WHERE {col} IS NOT NULL;"),
    ("Select only the {col} column from {table}.",
     "SELECT {col} FROM {table};"),
]


def _generate_challenge_fallback(parsed: dict, difficulty: str = "easy") -> tuple[str, str]:
    tables = parsed.get("tables", [])
    table  = random.choice(tables)
    tname  = table["name"]
    cols   = table.get("columns", [])
    col    = random.choice(cols)["name"] if cols else "id"
    tpl    = random.choice(_CHALLENGE_TEMPLATES)
    prompt = tpl[0].format(table=tname, col=col)
    sql    = tpl[1].format(table=tname, col=col)
    return prompt, sql


def _generate_challenge_with_gemini(parsed: dict, api_key: str, difficulty: str = "easy") -> tuple[str, str]:
    schema_desc = _build_schema_description(parsed)
    diff_instructions = {
        "easy":   "Make it easy: simple SELECT with basic WHERE or ORDER BY. No JOINs required.",
        "medium": "Make it medium: require at least one JOIN and a GROUP BY or aggregate function.",
        "hard":   "Make it hard: require multiple JOINs, subqueries or nested aggregations, and complex WHERE conditions.",
    }
    diff_note = diff_instructions.get(difficulty, diff_instructions["easy"])
    prompt_text = f"""{schema_desc}

Generate a single SQL challenge for a student learning SQL.
Difficulty: {difficulty.upper()} — {diff_note}
Return ONLY a JSON object with two keys:
  "prompt": a clear natural-language question (1 sentence)
  "sql":    the correct SQL answer for that question

Use only tables and columns that exist in the schema above.
Return ONLY the JSON, no markdown fences."""

    try:
        raw = _call_gemini(api_key, prompt_text)
        data = json.loads(_clean_json_response(raw))
        return data["prompt"], data["sql"]
    except Exception:
        return _generate_challenge_fallback(parsed)


def _grade_challenge_fallback(ref_sql: str, user_sql: str) -> tuple[int, str]:
    """Simple keyword-overlap scoring when no API key is available."""
    ref_tokens  = set(re.findall(r'\b\w+\b', ref_sql.upper()))
    user_tokens = set(re.findall(r'\b\w+\b', user_sql.upper()))
    if not ref_tokens:
        return 50, "Could not evaluate — reference SQL unavailable."
    overlap = len(ref_tokens & user_tokens) / len(ref_tokens)
    score = min(100, int(overlap * 110))
    if score >= 80:
        feedback = "Great job! Your query closely matches the expected answer."
    elif score >= 50:
        feedback = "Partially correct. Check your table/column names and clauses."
    else:
        feedback = "Needs improvement. Review the schema and try again."
    return score, feedback


def _grade_challenge_with_gemini(
    prompt_nl: str, ref_sql: str, user_sql: str, parsed: dict, api_key: str
) -> tuple[int, str]:
    schema_desc = _build_schema_description(parsed)
    grading_prompt = f"""{schema_desc}

Challenge prompt given to the student:
"{prompt_nl}"

Reference (correct) SQL:
{ref_sql}

Student's SQL:
{user_sql}

Grade the student's SQL query on a scale of 0–100. Consider:
- Correctness: does it answer the prompt?
- Table/column accuracy: are the names from the schema?
- Clause usage: WHERE, JOIN, ORDER BY used appropriately?
- Minor syntax issues are OK — focus on logic.

Return ONLY a JSON object:
{{
  "score": <integer 0-100>,
  "feedback": "<2-3 sentence friendly feedback explaining the grade>"
}}
No markdown, no extra text."""
    try:
        raw  = _call_gemini(api_key, grading_prompt)
        data = json.loads(_clean_json_response(raw))
        return int(data["score"]), str(data["feedback"])
    except Exception:
        return _grade_challenge_fallback(ref_sql, user_sql)


def _generate_hints_fallback(ref_sql: str, user_sql: str) -> list[str]:
    """Generate progressive hints by comparing SQL tokens."""
    ref_upper  = ref_sql.upper()
    user_upper = user_sql.upper()
    hints = []

    keywords = ["JOIN", "GROUP BY", "ORDER BY", "WHERE", "HAVING", "LIMIT",
                "SUM(", "COUNT(", "AVG(", "MAX(", "MIN(", "DISTINCT"]
    missing = [k for k in keywords if k in ref_upper and k not in user_upper]

    if missing:
        hints.append(f"Your query is missing: {', '.join(missing[:2])}.")
    else:
        hints.append("Check your column names — they may not match the schema exactly.")

    # Hint 2: table hint
    import re as _re
    ref_tables = _re.findall(r'FROM\s+(\w+)|JOIN\s+(\w+)', ref_upper)
    ref_tables = [t for pair in ref_tables for t in pair if t]
    if ref_tables:
        hints.append(f"Make sure you are using these tables: {', '.join(set(ref_tables))}.")
    else:
        hints.append("Re-read the prompt carefully and verify your WHERE conditions.")

    return hints


def _generate_hints_with_gemini(
    prompt_nl: str, ref_sql: str, user_sql: str, parsed: dict, api_key: str
) -> list[str]:
    """Generate 2 progressive hints without revealing the answer."""
    schema_desc = _build_schema_description(parsed)
    hint_prompt = f"""{schema_desc}

Challenge: "{prompt_nl}"

Reference SQL (DO NOT reveal this):
{ref_sql}

Student's SQL so far:
{user_sql}

Generate exactly 2 progressive hints to help the student without giving away the answer.
Hint 1 should be subtle (point to a missing clause or concept).
Hint 2 should be more specific (mention a table or function they are missing).

Return ONLY a JSON array of 2 strings:
["hint 1 text", "hint 2 text"]
No markdown, no extra text."""
    try:
        raw   = _call_gemini(api_key, hint_prompt)
        hints = json.loads(_clean_json_response(raw))
        if isinstance(hints, list) and len(hints) >= 2:
            return hints[:2]
        return _generate_hints_fallback(ref_sql, user_sql)
    except Exception:
        return _generate_hints_fallback(ref_sql, user_sql)



# ── SQL Challenge — clear history ─────────────────────────────
@app.route("/api/challenge/clear-history", methods=["POST"])
@login_required
def challenge_clear_history():
    ChallengeAttempt.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})



# ── Static uploads serving ─────────────────────────────────────────────────
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ══════════════════════════════════════════════════════════════════
# QUERY HISTORY — DELETE ALL
# ══════════════════════════════════════════════════════════════════
@app.route("/api/history/delete-all", methods=["POST"])
@login_required
def delete_all_history():
    QueryHistory.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════
# GLOBAL SEARCH API
# ══════════════════════════════════════════════════════════════════

LEARN_TOPICS = [
    ("What is a Database?",    "/learn?section=beginner"),
    ("What is SQL?",           "/learn?section=beginner&lesson=what-is-sql"),
    ("SELECT Statement",       "/learn?section=basic_sql"),
    ("WHERE Clause",           "/learn?section=basic_sql&lesson=where"),
    ("JOIN Tables",            "/learn?section=joins"),
    ("INNER JOIN",             "/learn?section=joins&lesson=inner"),
    ("LEFT JOIN",              "/learn?section=joins&lesson=left"),
    ("GROUP BY",               "/learn?section=intermediate"),
    ("ORDER BY",               "/learn?section=basic_sql&lesson=order-by"),
    ("HAVING Clause",          "/learn?section=intermediate&lesson=having"),
    ("Subqueries",             "/learn?section=advanced"),
    ("Indexes",                "/learn?section=advanced&lesson=indexes"),
    ("Primary Keys",           "/learn?section=beginner&lesson=primary-keys"),
    ("Foreign Keys",           "/learn?section=beginner&lesson=foreign-keys"),
    ("Aggregate Functions",    "/learn?section=intermediate&lesson=aggregates"),
    ("INSERT Statement",       "/learn?section=basic_sql&lesson=insert"),
    ("UPDATE Statement",       "/learn?section=basic_sql&lesson=update"),
    ("DELETE Statement",       "/learn?section=basic_sql&lesson=delete"),
    ("CREATE TABLE",           "/learn?section=basic_sql&lesson=create-table"),
    ("Transactions",           "/learn?section=advanced&lesson=transactions"),
]

STATIC_PAGES = [
    ("Dashboard",   "/dashboard",   "bi-grid",             "Overview of your schemas and queries"),
    ("Schemas",     "/schemas",     "bi-layers",           "Upload and manage ER diagram schemas"),
    ("Query",       "/query",       "bi-terminal",         "Generate SQL from natural language"),
    ("History",     "/history",     "bi-clock-history",    "View your past generated queries"),
    ("Challenges",  "/challenge",   "bi-lightning-charge", "Practice SQL with AI challenges"),
    ("Leaderboard", "/leaderboard", "bi-trophy",           "See how you rank against other users"),
    ("Badges",      "/badges",      "bi-award",            "Your earned achievement badges"),
    ("Learn",       "/learn",       "bi-book",             "Interactive SQL tutor and lessons"),
    ("Settings",    "/settings",    "bi-sliders",          "Manage your account and preferences"),
]


def _build_search_results(q: str, uid: int, limit: int = 14) -> list:
    """Return deduplicated, relevance-sorted search results for query q."""
    results = []
    seen_urls = set()
    seen_titles = set()

    def add(item):
        key = (item["url"], item["title"][:40].lower())
        if key not in seen_titles:
            seen_titles.add(key)
            results.append(item)

    # 1. Static pages — exact or partial match on page name
    for name, url, icon, desc in STATIC_PAGES:
        if q in name.lower() or q in desc.lower():
            add({"type": "page", "icon": icon, "title": name,
                 "subtitle": desc, "url": url, "score": 10})

    # 2. Schemas — search name and original filename
    schemas = SchemaUpload.query.filter_by(user_id=uid).order_by(
        SchemaUpload.uploaded_at.desc()).all()
    for s in schemas:
        name = (s.schema_name or s.original_name or "").strip()
        if q in name.lower():
            score = 9 if name.lower().startswith(q) else 7
            add({"type": "schema", "icon": "bi-layers",
                 "title": name or "Untitled Schema",
                 "subtitle": f"Schema · {s.uploaded_at.strftime('%b %d, %Y')}",
                 "url": f"/schemas/{s.id}", "score": score})

    # 3. Query history — search natural language and SQL text, deduplicate by title
    queries = QueryHistory.query.filter_by(user_id=uid).order_by(
        QueryHistory.created_at.desc()).limit(300).all()
    seen_q_titles = set()
    for qy in queries:
        nq = (qy.natural_query or "").strip()
        sql = (qy.generated_sql or "")
        nq_l = nq.lower()
        if q in nq_l or q in sql.lower():
            short = nq[:80]
            if short.lower() not in seen_q_titles:
                seen_q_titles.add(short.lower())
                score = 8 if nq_l.startswith(q) else 5
                add({"type": "query", "icon": "bi-terminal",
                     "title": short or "(empty query)",
                     "subtitle": f"Query · {qy.created_at.strftime('%b %d, %Y')}",
                     "url": "/history", "score": score})

    # 4. Challenge attempts — deduplicate by prompt prefix
    attempts = ChallengeAttempt.query.filter_by(user_id=uid).order_by(
        ChallengeAttempt.attempted_at.desc()).limit(150).all()
    seen_ch = set()
    for a in attempts:
        prompt = (a.prompt or "").strip()
        if q in prompt.lower():
            key = prompt[:50].lower()
            if key not in seen_ch:
                seen_ch.add(key)
                add({"type": "challenge", "icon": "bi-lightning-charge",
                     "title": prompt[:70] or "(challenge)",
                     "subtitle": f"Challenge · Score: {a.score or 0}/100",
                     "url": "/challenge", "score": 4})

    # 5. Learn topics — static list
    for topic, url in LEARN_TOPICS:
        if q in topic.lower():
            score = 6 if topic.lower().startswith(q) else 4
            add({"type": "lesson", "icon": "bi-book",
                 "title": topic, "subtitle": "SQL Learn · Lesson",
                 "url": url, "score": score})

    # Sort by score descending, then return top N
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:limit]


@app.route("/api/search")
@login_required
def global_search():
    """Full search: returns rich result cards with icons, subtitles, and URLs."""
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 1:
        return jsonify({"results": []})
    results = _build_search_results(q, current_user.id, limit=12)
    # Strip internal score field before sending to client
    for r in results:
        r.pop("score", None)
    return jsonify({"results": results})


@app.route("/api/search/suggest")
@login_required
def search_suggest():
    """Autocomplete: returns lightweight keyword suggestions for the dropdown."""
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 1:
        return jsonify({"suggestions": []})

    suggestions = []
    seen = set()

    def add_sug(text, category):
        key = text.lower()
        if key not in seen and len(suggestions) < 8:
            seen.add(key)
            suggestions.append({"text": text, "category": category})

    # Schema names
    schemas = SchemaUpload.query.filter_by(user_id=current_user.id).all()
    for s in schemas:
        name = (s.schema_name or s.original_name or "").strip()
        if name and q in name.lower():
            add_sug(name, "Schema")

    # Recent query natural language (unique)
    queries = QueryHistory.query.filter_by(user_id=current_user.id).order_by(
        QueryHistory.created_at.desc()).limit(200).all()
    for qy in queries:
        nq = (qy.natural_query or "").strip()
        if nq and q in nq.lower():
            add_sug(nq[:60], "Query")

    # Learn topic titles
    for topic, _ in LEARN_TOPICS:
        if q in topic.lower():
            add_sug(topic, "Lesson")

    # Static page names
    for name, _, _, _ in STATIC_PAGES:
        if q in name.lower():
            add_sug(name, "Page")

    return jsonify({"suggestions": suggestions})


# ══════════════════════════════════════════════════════════════════
# SAMPLE DATA GENERATION
# ══════════════════════════════════════════════════════════════════
def _generate_sample_data_heuristic(schema: dict, row_count: int = 5) -> dict:
    import random as _rand
    NAMES = ["Alice","Bob","Charlie","Diana","Eve","Frank","Grace","Henry","Iris","Jake"]
    EMAILS = ["gmail.com","yahoo.com","outlook.com","company.com","work.io"]
    WORDS = ["alpha","beta","gamma","delta","epsilon","zeta","theta","kappa"]
    COMPANIES = ["Acme Corp","TechStart","GlobalTech","NextGen","DataPro","CloudSys"]
    CITIES = ["Manila","Cebu","Davao","Quezon City","Makati","Pasig","Taguig","Iloilo"]
    result = {}
    for table in schema.get("tables",[]):
        tname = table["name"]
        rows = []
        for i in range(1, row_count + 1):
            row = {}
            for col in table.get("columns",[]):
                cname = col["name"]
                ctype = (col.get("type") or "TEXT").upper()
                cl = cname.lower()
                if col.get("primary_key"):
                    row[cname] = i
                elif cl in ("name","full_name","username","first_name","last_name"):
                    row[cname] = _rand.choice(NAMES) + (f"_{i}" if cl != "first_name" else "")
                elif "email" in cl:
                    row[cname] = f"user{i}@{_rand.choice(EMAILS)}"
                elif "phone" in cl:
                    row[cname] = f"+63-9{_rand.randint(100,999)}-{_rand.randint(1000,9999)}"
                elif "city" in cl or "location" in cl:
                    row[cname] = _rand.choice(CITIES)
                elif "company" in cl or "organization" in cl:
                    row[cname] = _rand.choice(COMPANIES)
                elif "price" in cl or "amount" in cl or "salary" in cl or "cost" in cl:
                    row[cname] = round(_rand.uniform(100,10000),2)
                elif "age" in cl:
                    row[cname] = _rand.randint(18,65)
                elif "quantity" in cl or "stock" in cl or "count" in cl:
                    row[cname] = _rand.randint(1,100)
                elif "score" in cl or "rating" in cl:
                    row[cname] = _rand.randint(1,10)
                elif cl.endswith("_id") and not col.get("primary_key"):
                    row[cname] = _rand.randint(1,row_count)
                elif "date" in cl or "created" in cl or "updated" in cl or "at" in cl:
                    row[cname] = f"2024-{_rand.randint(1,12):02d}-{_rand.randint(1,28):02d}"
                elif "status" in cl:
                    row[cname] = _rand.choice(["active","inactive","pending","completed"])
                elif ctype in ("INT","INTEGER","BIGINT","SMALLINT"):
                    row[cname] = _rand.randint(1,1000)
                elif ctype in ("FLOAT","DOUBLE","DECIMAL","NUMERIC","REAL"):
                    row[cname] = round(_rand.uniform(1,1000),2)
                elif ctype in ("BOOLEAN","BOOL"):
                    row[cname] = _rand.choice([True,False])
                else:
                    row[cname] = f"{_rand.choice(WORDS)}_{i}"
            rows.append(row)
        result[tname] = rows
    return result


def _build_insert_statements(sample_data: dict) -> str:
    statements = []
    for table, rows in sample_data.items():
        if not rows: continue
        cols = list(rows[0].keys())
        col_str = ", ".join(cols)
        statements.append(f"-- Table: {table}")
        for row in rows:
            vals = []
            for c in cols:
                v = row.get(c)
                if v is None: vals.append("NULL")
                elif isinstance(v, bool): vals.append("1" if v else "0")
                elif isinstance(v,(int,float)): vals.append(str(v))
                else: vals.append(f"'{str(v).replace(chr(39), chr(39)*2)}'")
            statements.append(f"INSERT INTO {table} ({col_str}) VALUES ({', '.join(vals)});")
        statements.append("")
    return "\n".join(statements)


@app.route("/api/generate-sample-data", methods=["POST"])
@login_required
def generate_sample_data():
    data = request.get_json()
    schema_id = data.get("schema_id")
    row_count = min(int(data.get("row_count",5)),20)
    if not schema_id:
        return jsonify({"error":"schema_id required"}),400
    schema_obj = SchemaUpload.query.filter_by(id=schema_id,user_id=current_user.id).first_or_404()
    raw = schema_obj.confirmed_schema or schema_obj.parsed_schema
    parsed = json.loads(raw) if raw else {}
    if not parsed.get("tables"):
        return jsonify({"error":"No tables in schema"}),400
    sample_data = _generate_sample_data_heuristic(parsed, row_count)
    if check_ollama_active():
        try:
            sd = _build_schema_description(parsed)
            prompt = f"{sd}\n\nGenerate {row_count} realistic rows per table. Return ONLY JSON: {{\"tablename\": [{{col: val}}]}}. No markdown."
            raw_resp = _call_ollama(prompt,"You are a database test data expert. Return ONLY valid JSON.")
            sample_data = json.loads(_clean_json_response(raw_resp))
        except Exception:
            pass
    insert_sql = _build_insert_statements(sample_data)
    return jsonify({"sample_data":sample_data,"insert_sql":insert_sql,"validation":{"valid":True,"message":f"Generated {sum(len(v) for v in sample_data.values())} rows"}})


@app.route("/api/validate-sql-live", methods=["POST"])
@login_required
def validate_sql_live():
    import sqlite3, tempfile, time
    data = request.get_json()
    sql = (data.get("sql") or "").strip()
    schema_id = data.get("schema_id")
    sample_data = data.get("sample_data", {})
    if not sql:
        return jsonify({"error":"SQL required"}),400
    parsed = {}
    if schema_id:
        so = SchemaUpload.query.filter_by(id=schema_id,user_id=current_user.id).first()
        if so:
            raw = so.confirmed_schema or so.parsed_schema
            if raw: parsed = json.loads(raw)
    if not parsed.get("tables"):
        return jsonify({"error":"No schema for validation"}),400
    with tempfile.NamedTemporaryFile(suffix=".db",delete=False) as tmp:
        tmp_path = tmp.name
    try:
        conn = sqlite3.connect(tmp_path)
        cur = conn.cursor()
        tmap = {"INT":"INTEGER","INTEGER":"INTEGER","BIGINT":"INTEGER","SMALLINT":"INTEGER","FLOAT":"REAL","DOUBLE":"REAL","DECIMAL":"REAL","BOOLEAN":"INTEGER","BOOL":"INTEGER","VARCHAR":"TEXT","CHAR":"TEXT","UUID":"TEXT","TIMESTAMP":"TEXT","DATETIME":"TEXT","DATE":"TEXT"}
        for t in parsed.get("tables",[]):
            cols = [f"{c['name']} {tmap.get((c.get('type') or 'TEXT').upper(),'TEXT')}{' PRIMARY KEY' if c.get('primary_key') else ''}" for c in t.get("columns",[])]
            try: cur.execute(f"CREATE TABLE IF NOT EXISTS {t['name']} ({', '.join(cols)})")
            except: pass
        sd = sample_data or _generate_sample_data_heuristic(parsed, 3)
        for tname,rows in sd.items():
            for row in rows:
                if not row: continue
                cs = list(row.keys())
                vs = [1 if v is True else (0 if v is False else v) for v in row.values()]
                try: cur.execute(f"INSERT OR IGNORE INTO {tname} ({','.join(cs)}) VALUES ({','.join(['?']*len(cs))})",vs)
                except: pass
        conn.commit()
        t0 = time.time()
        try:
            cur.execute(sql)
            rows = cur.fetchmany(10)
            elapsed = round((time.time()-t0)*1000,1)
            col_names = [d[0] for d in cur.description] if cur.description else []
            conn.close()
            return jsonify({"valid":True,"columns":col_names,"rows":[list(r) for r in rows],"execution_time_ms":elapsed,"message":f"✓ Query valid ({elapsed}ms)"})
        except Exception as e:
            conn.close()
            return jsonify({"valid":False,"error":str(e),"message":f"✗ {e}"})
    finally:
        try: os.unlink(tmp_path)
        except: pass


# ══════════════════════════════════════════════════════════════════
# SQL LEARN MODULE
# ══════════════════════════════════════════════════════════════════

SQL_LEARN_LESSONS = {
"beginner": {"title":"Beginner","icon":"bi-seedling","color":"#059669","lessons":[
        {"id":"what-is-db","title":"What is a Database?","content":"A database is an organized collection of structured data stored electronically. Think of it as a highly organized digital filing cabinet.\n\nDatabases are managed by software called a DBMS (Database Management System) — MySQL, PostgreSQL, SQLite, and SQL Server are popular examples.\n\nWhy use databases?\n- Store large amounts of data efficiently\n- Retrieve specific data quickly\n- Ensure data integrity and security\n- Allow multiple users to access data simultaneously\n- Support relationships between different data","example_schema":None,"example_query":None,"result":None},

        {"id":"what-is-sql","title":"What is SQL?","content":"SQL (Structured Query Language) is the standard language for managing relational databases.\n\nWith SQL you can:\n- Query data — retrieve information with SELECT\n- Insert new records with INSERT\n- Update existing data with UPDATE\n- Delete records with DELETE\n- Create database structures with CREATE\n\nSQL is case-insensitive but keywords are conventionally written in UPPERCASE for readability.","example_schema":"users (id INT PK, name TEXT, email TEXT)","example_query":"SELECT name, email FROM users;","result":[["Alice Smith","alice@example.com"],["Bob Jones","bob@example.com"]]},

        {"id":"tables-rows","title":"Tables and Rows","content":"Data is organized into tables (relations). A table is like a spreadsheet:\n\n- A table represents one type of entity (users, products, orders)\n- A row (record) = one instance of that entity\n- A column (field) = one attribute of the entity\n\nFor example, a users table has one row per user, and columns for name, email, age, etc.","example_schema":"users (id INT PK, name TEXT, email TEXT, age INT)","example_query":"SELECT * FROM users;","result":[[1,"Alice","alice@ex.com",28],[2,"Bob","bob@ex.com",34]]},
    ]},

        "basic_sql": {"title":"Basic SQL","icon":"bi-code-square","color":"#2563eb","lessons":[

        {"id":"select","title":"SELECT Statement","content":"The SELECT statement retrieves data from tables. It's the most fundamental SQL command.\n\n```sql\nSELECT column1, column2 FROM table_name;\n```\n\nUse SELECT * to get all columns. For specific columns, list them by name — this is more efficient and explicit.\n\nColumn aliases rename output columns:\n```sql\nSELECT name AS full_name, email AS contact FROM users;\n```","example_schema":"products (id INT PK, name TEXT, price DECIMAL, stock INT)","example_query":"SELECT name, price FROM products WHERE price < 100;","result":[["Widget A",49.99],["Gadget B",89.00]]},

        {"id":"where","title":"WHERE Clause","content":"The WHERE clause filters rows based on conditions.\n\nCommon operators:\n- = equal, != or <> not equal\n- >, <, >=, <= comparison\n- LIKE pattern matching (% = any text)\n- IN (a, b, c) match a list\n- BETWEEN x AND y range\n- IS NULL / IS NOT NULL\n- AND, OR, NOT combine conditions","example_schema":"users (id, name, age, city)","example_query":"SELECT name, age FROM users WHERE age >= 25 AND city = 'Manila';","result":[["Grace",27],["Henry",31]]},

        {"id":"order-by","title":"ORDER BY","content":"ORDER BY sorts results.\n\n- ASC = ascending (1→9, A→Z) — default\n- DESC = descending (9→1, Z→A)\n\nSort by multiple columns:\n```sql\nSELECT * FROM products ORDER BY category ASC, price DESC;\n```\n\nORDER BY always comes after WHERE.","example_schema":"employees (id, name, department, salary)","example_query":"SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 3;","result":[["Diana",95000],["Frank",88000],["Alice",82000]]},

        {"id":"limit","title":"LIMIT","content":"LIMIT restricts the number of rows returned. Essential for previewing data and pagination.\n\n```sql\nSELECT * FROM products LIMIT 10;\n```\n\nWith OFFSET for pagination:\n```sql\n-- Page 2 of 10 results per page\nSELECT * FROM products LIMIT 10 OFFSET 10;\n```","example_schema":"orders (id, customer, product, amount, created_at)","example_query":"SELECT customer, product, amount FROM orders ORDER BY amount DESC LIMIT 5;","result":[["Alice","Laptop",1200],["Bob","Monitor",350],["Charlie","Keyboard",89]]},
    ]},

        "intermediate": {"title":"Intermediate SQL","icon":"bi-graph-up","color":"#7c3aed","lessons":[

        {"id":"group-by","title":"GROUP BY","content":"GROUP BY groups rows with the same values so you can run aggregate calculations per group.\n\nAggregate functions:\n- COUNT() — count rows\n- SUM() — total\n- AVG() — average\n- MIN() / MAX() — min/max value\n\nRule: Columns in SELECT not inside an aggregate must appear in GROUP BY.","example_schema":"orders (id, customer_id, product, amount, status)","example_query":"SELECT status, COUNT(*) as total, SUM(amount) as revenue FROM orders GROUP BY status;","result":[["completed",45,12500.00],["pending",12,3200.00],["cancelled",5,890.00]]},

        {"id":"having","title":"HAVING","content":"HAVING filters the results of GROUP BY — like WHERE but for grouped data.\n\n- WHERE filters rows before grouping\n- HAVING filters groups after aggregation\n\n```sql\nSELECT department, COUNT(*) as emp_count\nFROM employees\nGROUP BY department\nHAVING COUNT(*) > 10;\n```","example_schema":"sales (id, rep_name, region, amount)","example_query":"SELECT region, AVG(amount) as avg_sale FROM sales GROUP BY region HAVING AVG(amount) > 5000;","result":[["North",7800.50],["South",6250.00]]},
    ]},

    "joins": {"title":"SQL Joins","icon":"bi-diagram-3","color":"#dc2626","lessons":[

    {"id":"inner-join","title":"INNER JOIN","content":"An INNER JOIN returns rows matching in both tables. Non-matching rows are excluded.\n\n```sql\nSELECT t1.col, t2.col\nFROM table1 t1\nINNER JOIN table2 t2 ON t1.id = t2.table1_id;\n```\n\nThink of it as the intersection — only rows present in both tables appear.","example_schema":"users (id, name) + orders (id, user_id, product, amount)","example_query":"SELECT u.name, o.product, o.amount FROM users u INNER JOIN orders o ON u.id = o.user_id;","result":[["Alice","Laptop",1200],["Bob","Mouse",25],["Alice","Monitor",350]]},

    {"id":"left-join","title":"LEFT JOIN","content":"A LEFT JOIN returns all rows from the left table and matched rows from the right. No match → right columns are NULL.\n\nUse it to include records even without related data.\n\n```sql\nSELECT u.name, o.product\nFROM users u\nLEFT JOIN orders o ON u.id = o.user_id;\n```","example_schema":"users (id, name) + orders (id, user_id, product)","example_query":"SELECT u.name, COALESCE(o.product, 'No orders') as product FROM users u LEFT JOIN orders o ON u.id = o.user_id;","result":[["Alice","Laptop"],["Bob","Mouse"],["Charlie","No orders"]]},
    ]},

        "advanced": {"title":"Advanced SQL","icon":"bi-cpu","color":"#0891b2","lessons":[

        {"id":"subqueries","title":"Subqueries","content":"A subquery is a query nested inside another query.\n\nTypes:\n- Scalar — returns a single value\n- Column — returns a column\n- Table — returns a result set (used in FROM)\n\n```sql\nSELECT name, salary\nFROM employees\nWHERE salary > (SELECT AVG(salary) FROM employees);\n```","example_schema":"products (id, name, price, category_id)","example_query":"SELECT name, price FROM products WHERE price > (SELECT AVG(price) FROM products) ORDER BY price DESC;","result":[["Premium Widget",299.99],["Pro Tool",189.00]]},

        {"id":"indexes","title":"Indexes","content":"An index speeds up data retrieval at the cost of storage and slower writes.\n\nWhen to index:\n- Columns in WHERE clauses\n- Columns used in JOINs\n- Columns in ORDER BY\n\n```sql\nCREATE INDEX idx_email ON users(email);\n\nCREATE UNIQUE INDEX idx_user_email ON users(email);\n```\n\n⚠️ Too many indexes slow INSERT/UPDATE/DELETE.","example_schema":"users (id, name, email, created_at)","example_query":"CREATE INDEX idx_users_email ON users(email);","result":None},
    ]}
}


@app.route("/learn")
@login_required
def learn():
    section = request.args.get("section","beginner")
    lesson_id = request.args.get("lesson")
    current_section = SQL_LEARN_LESSONS.get(section, SQL_LEARN_LESSONS["beginner"])
    current_lesson = None
    if lesson_id:
        for l in current_section["lessons"]:
            if l["id"] == lesson_id:
                current_lesson = l
                break
    if not current_lesson and current_section["lessons"]:
        current_lesson = current_section["lessons"][0]
    # Compute progress per section (session-based simple tracking via query count heuristic)
    return render_template("learn.html", sections=SQL_LEARN_LESSONS, current_section_key=section, current_section=current_section, current_lesson=current_lesson)


@app.route("/api/learn/ask-tutor", methods=["POST"])
@login_required
def learn_ask_tutor():
    data = request.get_json()
    question = (data.get("question") or "").strip()

    if not question:
        return jsonify({"error": "Question required"}), 400

    system = (
        "You are a friendly SQL tutor for beginners. "
        "Give clear, concise explanations with short SQL examples. "
        "Keep responses under 250 words."
    )

    prompt = (
        f"Student question: {question}\n\n"
        "Explain this SQL concept clearly for a beginner, "
        "with a short example if helpful."
    )

    answer = ""

    # Try Ollama first
    if check_ollama_active():
        try:
            answer = _call_ollama(prompt, system=system)
        except Exception:
            pass

    # Fallback to Gemini if Ollama fails
    if not answer and _get_gemini_key():
        try:
            answer = _call_gemini(
                _get_gemini_key(),
                f"{system}\n\n{prompt}"
            )
        except Exception:
            pass

    # Final fallback message
    if not answer:
        answer = (
            f"I'd love to explain '{question}'! "
            "AI explanations are temporarily unavailable — please make sure "
            "Ollama is running, or try again in a moment."
        )

    return jsonify({"answer": answer})


# ══════════════════════════════════════════════════════════════════
# SQL COMPILER — Isolated practice database
# ══════════════════════════════════════════════════════════════════

import sqlite3
import tempfile
import threading

# Thread-local storage for practice DB connections
_practice_db_lock = threading.Lock()
# Per-user persistent in-memory practice DBs: {user_id: sqlite3.Connection}
# INSERT/DELETE/UPDATE changes are kept alive for the session so students can
# see the effect of their mutations.  A user can call /api/sql-compiler/reset
# to restore the original seed data at any time.
_practice_db_store: dict = {}

# ── Sample database schema & seed data ────────────────────────────────────
PRACTICE_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id   INTEGER PRIMARY KEY,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    course       TEXT NOT NULL,
    year_level   INTEGER DEFAULT 1,
    gpa          REAL DEFAULT 0.0,
    email        TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS courses (
    course_id    INTEGER PRIMARY KEY,
    course_code  TEXT NOT NULL UNIQUE,
    course_name  TEXT NOT NULL,
    department   TEXT NOT NULL,
    units        INTEGER DEFAULT 3,
    professor    TEXT
);

CREATE TABLE IF NOT EXISTS enrollments (
    enrollment_id INTEGER PRIMARY KEY,
    student_id    INTEGER NOT NULL,
    course_id     INTEGER NOT NULL,
    grade         REAL,
    semester      TEXT DEFAULT '1st',
    school_year   TEXT DEFAULT '2024-2025',
    FOREIGN KEY (student_id) REFERENCES students(student_id),
    FOREIGN KEY (course_id)  REFERENCES courses(course_id)
);

CREATE TABLE IF NOT EXISTS departments (
    dept_id      INTEGER PRIMARY KEY,
    dept_code    TEXT NOT NULL UNIQUE,
    dept_name    TEXT NOT NULL,
    dean         TEXT,
    building     TEXT
);

CREATE TABLE IF NOT EXISTS professors (
    prof_id      INTEGER PRIMARY KEY,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    dept_id      INTEGER,
    email        TEXT UNIQUE,
    specialization TEXT,
    FOREIGN KEY (dept_id) REFERENCES departments(dept_id)
);
"""

PRACTICE_DB_SEED = """
INSERT OR IGNORE INTO departments VALUES (1,'CS','Computer Science','Dr. Ana Reyes','Eng Building A');
INSERT OR IGNORE INTO departments VALUES (2,'IT','Information Technology','Dr. Ramon Cruz','Eng Building B');
INSERT OR IGNORE INTO departments VALUES (3,'IS','Information Systems','Dr. Lorna Santos','Eng Building C');

INSERT OR IGNORE INTO professors VALUES (1,'Marco','Dela Cruz',1,'m.delacruz@tip.edu.ph','Database Systems');
INSERT OR IGNORE INTO professors VALUES (2,'Liza','Gomez',2,'l.gomez@tip.edu.ph','Web Development');
INSERT OR IGNORE INTO professors VALUES (3,'Jose','Hernandez',1,'j.hernandez@tip.edu.ph','Data Structures');
INSERT OR IGNORE INTO professors VALUES (4,'Maria','Tan',3,'m.tan@tip.edu.ph','Systems Analysis');
INSERT OR IGNORE INTO professors VALUES (5,'Kevin','Ramos',2,'k.ramos@tip.edu.ph','Network Security');

INSERT OR IGNORE INTO students VALUES (1,'John','Cruz','BSCS',2,1.75,'john.cruz@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (2,'Maria','Santos','BSIT',3,1.50,'maria.santos@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (3,'Kevin','Reyes','BSCS',2,1.25,'kevin.reyes@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (4,'Ana','Bautista','BSIS',1,0.00,'ana.bautista@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (5,'Miguel','Lim','BSIT',4,1.50,'miguel.lim@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (6,'Rosa','Aquino','BSCS',3,2.00,'rosa.aquino@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (7,'Carlo','Villanueva','BSIS',2,1.75,'carlo.villanueva@student.tip.edu.ph');
INSERT OR IGNORE INTO students VALUES (8,'Lea','Garcia','BSIT',1,0.00,'lea.garcia@student.tip.edu.ph');

INSERT OR IGNORE INTO courses VALUES (101,'CCS309','Parallel and Distributed Computing','Computer Science',3,'Prof. Marco Dela Cruz');
INSERT OR IGNORE INTO courses VALUES (102,'CCS202','Web Development','Information Technology',3,'Prof. Liza Gomez');
INSERT OR IGNORE INTO courses VALUES (103,'CCS201','Data Structures and Algorithms','Computer Science',3,'Prof. Jose Hernandez');
INSERT OR IGNORE INTO courses VALUES (104,'IS301','Systems Analysis and Design','Information Systems',3,'Prof. Maria Tan');
INSERT OR IGNORE INTO courses VALUES (105,'CCS401','Database Systems','Computer Science',3,'Prof. Marco Dela Cruz');
INSERT OR IGNORE INTO courses VALUES (106,'IT305','Network Security','Information Technology',3,'Prof. Kevin Ramos');

INSERT OR IGNORE INTO enrollments VALUES (1,1,101,1.50,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (2,2,102,1.75,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (3,3,103,1.25,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (4,1,105,1.25,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (5,5,102,1.50,'2nd','2023-2024');
INSERT OR IGNORE INTO enrollments VALUES (6,6,101,2.00,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (7,2,104,1.75,'2nd','2023-2024');
INSERT OR IGNORE INTO enrollments VALUES (8,3,105,1.25,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (9,7,104,1.75,'1st','2024-2025');
INSERT OR IGNORE INTO enrollments VALUES (10,5,106,1.50,'1st','2024-2025');
"""

# Restricted SQL keywords (DML that modifies schema)
_SQL_FORBIDDEN = re.compile(
    r'\b(DROP|TRUNCATE|ALTER|ATTACH|DETACH|PRAGMA|VACUUM)\b',
    re.IGNORECASE
)

# Allowed write ops (only for the practice DB)
_SQL_WRITE_OPS = re.compile(
    r'^\s*(INSERT|UPDATE|DELETE|CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS)\b',
    re.IGNORECASE
)

def _get_practice_db(user_id=None) -> sqlite3.Connection:
    """Return a persistent in-memory practice DB for the given user.

    The first call for a user creates and seeds the DB.  Subsequent calls
    return the same connection so that INSERT/DELETE/UPDATE changes persist
    within a user's session.  Pass user_id=None (or omit) to get a fresh
    isolated DB (used only by internal helpers that don't mutate data).
    """
    if user_id is None:
        # Isolated fresh DB – used only for read-only helper calls
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(PRACTICE_DB_SCHEMA)
        conn.executescript(PRACTICE_DB_SEED)
        conn.commit()
        return conn

    with _practice_db_lock:
        if user_id not in _practice_db_store:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(PRACTICE_DB_SCHEMA)
            conn.executescript(PRACTICE_DB_SEED)
            conn.commit()
            _practice_db_store[user_id] = conn
        return _practice_db_store[user_id]


def _reset_practice_db(user_id) -> None:
    """Drop and re-seed the practice DB for a user, restoring original data."""
    with _practice_db_lock:
        if user_id in _practice_db_store:
            old = _practice_db_store.pop(user_id)
            try:
                old.close()
            except Exception:
                pass
    # Re-create immediately so the next call is instant
    _get_practice_db(user_id)

# Session-scoped practice DB (persistent per user, fresh per new user)
def _execute_practice_sql(sql: str, user_id=None) -> dict:
    """
    Execute SQL against the persistent per-user practice database.
    INSERT/DELETE/UPDATE changes are preserved across requests so students
    can observe the effect of their mutations.
    Returns columns, rows, rowcount, duration, and any error messages.
    """
    sql = sql.strip()
    if not sql:
        return {"error": "Empty query.", "columns": [], "rows": [], "rowcount": 0, "duration_ms": 0}

    # Security: block forbidden keywords
    if _SQL_FORBIDDEN.search(sql):
        return {
            "error": "⛔ Query blocked: DROP, TRUNCATE, ALTER, ATTACH, DETACH, PRAGMA, and VACUUM are not allowed in the practice environment.",
            "columns": [], "rows": [], "rowcount": 0, "duration_ms": 0
        }

    conn = _get_practice_db(user_id)
    close_after = user_id is None  # only close fresh/isolated connections
    try:
        t0 = time.time()
        cursor = conn.cursor()
        # Split multiple statements
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        results = []
        last_desc = None
        total_rowcount = 0

        for stmt in statements:
            try:
                cursor.execute(stmt)
                if cursor.description:
                    last_desc = cursor.description
                    rows = cursor.fetchall()
                    results = [dict(r) for r in rows]
                    total_rowcount = len(results)
                else:
                    conn.commit()
                    total_rowcount = cursor.rowcount if cursor.rowcount >= 0 else 0
                    results = []
                    last_desc = None
            except sqlite3.Error as e:
                duration_ms = round((time.time() - t0) * 1000, 2)
                return {"error": str(e), "columns": [], "rows": [], "rowcount": 0, "duration_ms": duration_ms}

        duration_ms = round((time.time() - t0) * 1000, 2)
        columns = [col[0] for col in last_desc] if last_desc else []

        # Detect which table was mutated so the frontend can auto-refresh its preview
        mutated_table = None
        mutation_op = None
        first_stmt = statements[0].upper() if statements else ""
        for op in ("INSERT", "DELETE", "UPDATE"):
            if first_stmt.startswith(op):
                mutation_op = op
                break
        if mutation_op:
            import re as _re
            m = _re.search(
                r'(?:INTO|FROM|UPDATE)\s+([a-zA-Z_][a-zA-Z0-9_]*)',
                statements[0], _re.IGNORECASE
            )
            mutated_table = m.group(1).lower() if m else None

        return {
            "columns": columns,
            "rows": results,
            "rowcount": total_rowcount,
            "duration_ms": duration_ms,
            "error": None,
            "mutation_op": mutation_op,
            "mutated_table": mutated_table,
        }
    except Exception as e:
        logger.error("SQL Compiler execution error: %s", e)
        return {"error": f"Execution error: {e}", "columns": [], "rows": [], "rowcount": 0, "duration_ms": 0}
    finally:
        if close_after:
            conn.close()


def _get_practice_schema_info(user_id=None) -> list[dict]:
    """Return schema info for all practice tables (columns, types)."""
    conn = _get_practice_db(user_id)
    try:
        tables_cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in tables_cursor.fetchall()]
        schema_info = []
        for table in tables:
            cols_cursor = conn.execute(f"PRAGMA table_info({table})")
            columns = [
                {"name": col[1], "type": col[2], "pk": bool(col[5]), "notnull": bool(col[3])}
                for col in cols_cursor.fetchall()
            ]
            # Row count
            count_cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            row_count = count_cur.fetchone()[0]
            schema_info.append({"table": table, "columns": columns, "row_count": row_count})
        return schema_info
    finally:
        if user_id is None:
            conn.close()


@app.route("/sql-compiler")
@login_required
def sql_compiler():
    schema_info = _get_practice_schema_info(current_user.id)
    sample_queries = [
        {"label": "All Students", "sql": "SELECT * FROM students;"},
        {"label": "All Courses", "sql": "SELECT * FROM courses;"},
        {"label": "All Enrollments", "sql": "SELECT * FROM enrollments;"},
        {"label": "Student with Enrollments (JOIN)", "sql": "SELECT s.first_name, s.last_name, c.course_name, e.grade\nFROM students s\nJOIN enrollments e ON s.student_id = e.student_id\nJOIN courses c ON e.course_id = c.course_id\nORDER BY s.last_name;"},
        {"label": "Average Grade per Course", "sql": "SELECT c.course_name, ROUND(AVG(e.grade), 2) AS avg_grade, COUNT(*) AS enrolled\nFROM courses c\nJOIN enrollments e ON c.course_id = e.course_id\nGROUP BY c.course_id\nORDER BY avg_grade;"},
        {"label": "Top Students (grade ≤ 1.5)", "sql": "SELECT first_name, last_name, course, gpa\nFROM students\nWHERE gpa > 0 AND gpa <= 1.5\nORDER BY gpa;"},
        {"label": "Courses with no Enrollments", "sql": "SELECT c.course_name\nFROM courses c\nLEFT JOIN enrollments e ON c.course_id = e.course_id\nWHERE e.enrollment_id IS NULL;"},
        {"label": "Students per Course (GROUP BY)", "sql": "SELECT c.course_name, COUNT(e.student_id) AS total_students\nFROM courses c\nLEFT JOIN enrollments e ON c.course_id = e.course_id\nGROUP BY c.course_id\nHAVING total_students > 0\nORDER BY total_students DESC;"},
        {"label": "Subquery: Above-average GPA", "sql": "SELECT first_name, last_name, gpa\nFROM students\nWHERE gpa > (SELECT AVG(gpa) FROM students WHERE gpa > 0)\nORDER BY gpa;"},
        {"label": "INSERT a Student", "sql": "INSERT INTO students (student_id, first_name, last_name, course, year_level, gpa, email)\nVALUES (9, 'Paolo', 'Mendoza', 'BSCS', 1, 0.0, 'p.mendoza@student.tip.edu.ph');"},
        {"label": "UPDATE a Grade", "sql": "UPDATE enrollments SET grade = 1.25 WHERE enrollment_id = 1;"},
        {"label": "DELETE an Enrollment", "sql": "DELETE FROM enrollments WHERE enrollment_id = 10;"},
    ]
    return render_template("sql_compiler.html", schema_info=schema_info, sample_queries=sample_queries)


@app.route("/api/sql-compiler/execute", methods=["POST"])
@login_required
def sql_compiler_execute():
    data = request.get_json()
    sql  = (data.get("sql") or "").strip()
    if not sql:
        return jsonify({"error": "No SQL provided.", "columns": [], "rows": [], "rowcount": 0, "duration_ms": 0}), 400
    result = _execute_practice_sql(sql, user_id=current_user.id)
    status_code = 400 if result.get("error") else 200
    return jsonify(result), status_code


@app.route("/api/sql-compiler/schema", methods=["GET"])
@login_required
def sql_compiler_schema():
    return jsonify({"tables": _get_practice_schema_info(current_user.id)})


@app.route("/api/sql-compiler/preview/<table_name>", methods=["GET"])
@login_required
def sql_compiler_preview(table_name):
    # Validate table name (alphanumeric + underscore only)
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
        return jsonify({"error": "Invalid table name."}), 400
    allowed_tables = {t["table"] for t in _get_practice_schema_info(current_user.id)}
    if table_name not in allowed_tables:
        return jsonify({"error": "Table not found."}), 404
    result = _execute_practice_sql(f"SELECT * FROM {table_name} LIMIT 50", user_id=current_user.id)
    return jsonify(result)

@app.route("/api/sql-compiler/reset", methods=["POST"])
@login_required
def sql_compiler_reset():
    """Restore the practice DB to its original seeded state for the current user."""
    _reset_practice_db(current_user.id)
    return jsonify({"ok": True, "message": "Practice database reset to original data."})


@app.route("/api/sql-compiler/ai-help", methods=["POST"])
@login_required
def sql_compiler_ai_help():
    """Use Gemini (server-side key) to explain or fix a SQL query."""
    data    = request.get_json()
    sql     = (data.get("sql") or "").strip()
    mode    = data.get("mode", "explain")  # explain | fix | generate
    context = (data.get("context") or "").strip()
    api_key = _get_gemini_key()

    schema_summary = "\n".join(
        f"- {t['table']} ({', '.join(c['name'] for c in t['columns'])})"
        for t in _get_practice_schema_info(current_user.id)
    )

    if mode == "explain":
        prompt = f"Explain this SQL query in plain English for a student beginner:\n\n```sql\n{sql}\n```\n\nDatabase tables available:\n{schema_summary}\n\nKeep it under 150 words. Be friendly and educational."
    elif mode == "fix":
        prompt = f"This SQL query has an error:\n\n```sql\n{sql}\n```\nError: {context}\n\nDatabase tables:\n{schema_summary}\n\nProvide the corrected SQL and briefly explain what was wrong. Format: corrected SQL first, then explanation."
    else:
        prompt = f"Generate a SQL query for: {context}\n\nDatabase tables:\n{schema_summary}\n\nReturn only the SQL query, no explanation."

    if not api_key:
        logger.error("SQL Compiler AI help requested but GEMINI_API_KEY is not configured.")
        return jsonify({"error": "The AI assistant is temporarily unavailable. Please try again later."}), 503

    try:
        answer = _call_gemini(api_key, prompt)
        return jsonify({"answer": answer})
    except Exception as e:
        logger.error("SQL Compiler AI help error: %s", e)
        return jsonify({"error": "The AI assistant is temporarily unavailable. Please try again later."}), 503


# ── Init ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()
    run_migrations()

if __name__ == "__main__":
    app.run(debug=True, port=5000)