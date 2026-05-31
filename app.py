import os
import json
import re
import datetime
from pathlib import Path

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import text

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

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

# ── Models ─────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(120), nullable=False)
    email        = db.Column(db.String(200), unique=True, nullable=False)
    password     = db.Column(db.String(256), nullable=False)
    company      = db.Column(db.String(120), default="")
    role         = db.Column(db.String(80), default="")
    gemini_key   = db.Column(db.String(256), default="")
    created_at   = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    agreement_accepted = db.Column(db.Boolean, default=False)
    agreement_accepted_at = db.Column(db.DateTime, nullable=True)
    schemas      = db.relationship("SchemaUpload", backref="owner", lazy=True,
                                   cascade="all, delete-orphan")
    queries      = db.relationship("QueryHistory", backref="owner", lazy=True,
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

# ── DB Migration: add new columns if missing ───────────────────────────────
def run_migrations():
    """Add new columns to existing tables without destroying data."""
    with db.engine.connect() as conn:
        migrations = [
            ("schema_uploads", "confirmed_schema", "TEXT DEFAULT ''"),
            ("users", "agreement_accepted", "INTEGER DEFAULT 0"),
            ("users", "agreement_accepted_at", "DATETIME"),
            ("query_history", "sql_explanation", "TEXT DEFAULT ''"),
            ("query_history", "accuracy_score", "REAL"),
            ("query_history", "accuracy_notes", "TEXT DEFAULT ''"),
            ("query_history", "conversation_context", "TEXT DEFAULT ''"),
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
            except Exception:
                pass  # Column already exists


# ── Jinja Filters ──────────────────────────────────────────────────────────
@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


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


MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

def _call_gemini(api_key: str, contents) -> str:
    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        for model_name in MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                )
                return response.text
            except Exception:
                pass

    except Exception:
        pass

    # Legacy fallback
    import google.generativeai as genai_legacy

    genai_legacy.configure(api_key=api_key)

    for model_name in MODELS:
        try:
            model = genai_legacy.GenerativeModel(model_name)
            response = model.generate_content(contents)
            return response.text
        except Exception:
            pass

    raise RuntimeError("No working Gemini model found")


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


# ── SQL generation ─────────────────────────────────────────────────────────

def generate_sql_with_gemini(natural_query: str, schema: dict, api_key: str) -> str:
    """Generate SQL from natural language using Gemini, schema-aware."""
    schema_desc = _build_schema_description(schema)
    prompt = f"""{schema_desc}

User Request: {natural_query}

Generate a single, correct SQL query that satisfies the user's request.
Use standard SQL syntax. Use table aliases where appropriate.
Return ONLY the SQL query. No explanation. No markdown fences."""

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.0-flash", contents=prompt
        )
        return _clean_json_response(response.text).lstrip("sql").strip()
    except Exception:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        model = genai_legacy.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        return _clean_json_response(response.text).lstrip("sql").strip()


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
# FEATURE 2: Plain-English SQL Explanation
# ══════════════════════════════════════════════════════════════════
def explain_sql_with_gemini(sql: str, api_key: str) -> str:
    """Convert SQL into a plain English explanation for non-technical users."""
    prompt = f"""You are a helpful database assistant explaining SQL to a non-technical person.

Given this SQL query:
{sql}

Write a clear, friendly explanation in 2-4 sentences that:
- Describes what data will be retrieved/modified
- Mentions any filters or conditions in plain English
- Avoids technical jargon
- Starts with "This query..."

Return ONLY the explanation text, nothing else."""
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return response.text.strip()
    except Exception:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        model = genai_legacy.GenerativeModel("gemini-2.0-flash")
        return model.generate_content(prompt).text.strip()


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


def _ai_evaluate_sql(sql: str, natural_query: str, schema: dict, api_key: str) -> dict:
    schema_desc = _build_schema_description(schema)
    prompt = f"""You are a senior database developer reviewing a generated SQL query.

{schema_desc}

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
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        raw = _clean_json_response(response.text)
        return json.loads(raw)
    except Exception:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        model = genai_legacy.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        raw = _clean_json_response(response.text)
        return json.loads(raw)


# ══════════════════════════════════════════════════════════════════
# FEATURE 3: Conversational Query Refinement
# ══════════════════════════════════════════════════════════════════
def refine_sql_with_conversation(
    original_sql: str,
    refinement_request: str,
    conversation_history: list,
    schema: dict,
    api_key: str
) -> str:
    """Refine an existing SQL query based on follow-up natural language instructions."""
    schema_desc = _build_schema_description(schema)
    history_str = ""
    for turn in conversation_history[-6:]:
        role = "User" if turn["role"] == "user" else "System"
        history_str += f"{role}: {turn['content']}\n"

    prompt = f"""You are an intelligent SQL assistant helping refine queries through conversation.

{schema_desc}

Conversation history:
{history_str}

Current SQL query:
{original_sql}

User's refinement request: "{refinement_request}"

Modify the SQL query to incorporate the user's refinement.
Keep all previous conditions unless the user explicitly asks to remove them.
Return ONLY the updated SQL query, no explanation, no markdown fences."""

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return _clean_json_response(response.text).lstrip("sql").strip()
    except Exception:
        import google.generativeai as genai_legacy
        genai_legacy.configure(api_key=api_key)
        model = genai_legacy.GenerativeModel("gemini-2.0-flash")
        raw = model.generate_content(prompt).text.strip()
        return raw.lstrip("```sql").lstrip("```").rstrip("```").strip()


# ══════════════════════════════════════════════════════════════════
# AGREEMENT ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/agreement")
@login_required
def agreement_page():
    """Show Terms & Privacy agreement screen."""
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("signup"))
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for("signup"))
        user = User(name=name, email=email,
                    password=generate_password_hash(password, method="scrypt"))
        db.session.add(user)
        db.session.commit()
        flash("Account created! Please sign in.", "success")
        return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user, remember=remember)
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
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
    ocr_text = run_ocr(filepath)
    parsed = {}
    api_key = current_user.gemini_key
    if api_key:
        try:
            # Pass the actual image path — Gemini Vision gives far better results
            parsed = parse_schema_with_gemini(ocr_text, api_key, image_path=filepath)
            # Validate we got real tables, not type-keyword garbage
            if not parsed.get("tables"):
                raise ValueError("No tables extracted from image — trying OCR fallback")
        except Exception as exc:
            try:
                # Second attempt: text-only Gemini
                parsed = parse_schema_with_gemini(ocr_text, api_key, image_path=None)
                if not parsed.get("tables"):
                    raise ValueError("Text-only parse also produced no tables")
            except Exception as exc2:
                parsed = parse_schema_fallback(ocr_text)
                flash(
                    f"AI parsing encountered issues ({exc2}); used heuristic parser. "
                    "Review the schema on the next page and correct any errors.",
                    "warning"
                )
    else:
        parsed = parse_schema_fallback(ocr_text)
        if parsed.get("tables"):
            flash(
                "No Gemini API key — used heuristic parser. "
                "Add your key in Settings for AI-powered accuracy.",
                "info"
            )
        else:
            flash(
                "No Gemini API key and heuristic parser found no tables. "
                "Please add tables manually on the confirmation page, or add a Gemini API key.",
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

    api_key = current_user.gemini_key
    sql = ""
    model_used = ""

    if api_key and parsed.get("tables"):
        try:
            sql = generate_sql_with_gemini(natural_query, parsed, api_key)
            model_used = "gemini-2.0-flash"
        except Exception as exc:
            return jsonify({"error": f"Gemini error: {exc}"}), 500
    else:
        sql = _fallback_sql(natural_query, parsed)
        model_used = "heuristic"

    # Feature 2: Generate explanation
    explanation = ""
    if api_key:
        try:
            explanation = explain_sql_with_gemini(sql, api_key)
        except Exception:
            explanation = explain_sql_fallback(sql)
    else:
        explanation = explain_sql_fallback(sql)

    # Feature 4: Evaluate accuracy
    eval_result = evaluate_sql_accuracy(sql, natural_query, parsed, api_key if api_key else None)

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

    return jsonify({
        "sql": sql,
        "model": model_used,
        "query_id": history.id,
        "explanation": explanation,
        "evaluation": eval_result
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

    api_key = current_user.gemini_key
    if not api_key:
        return jsonify({"error": "Gemini API key required for query refinement."}), 400

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
            parsed,
            api_key
        )
    except Exception as exc:
        return jsonify({"error": f"Refinement failed: {exc}"}), 500

    # Update conversation
    conversation.append({"role": "assistant", "content": new_sql})

    # Feature 2: New explanation
    try:
        explanation = explain_sql_with_gemini(new_sql, api_key)
    except Exception:
        explanation = explain_sql_fallback(new_sql)

    # Feature 4: Re-evaluate
    eval_result = evaluate_sql_accuracy(new_sql, refinement, parsed, api_key)

    # Save refined query as new history entry
    new_history = QueryHistory(
        user_id=current_user.id,
        schema_id=schema_obj.id if schema_obj else history_obj.schema_id,
        natural_query=f"[Refined] {refinement}",
        generated_sql=new_sql,
        model_used="gemini-2.0-flash",
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
    api_key = current_user.gemini_key
    if api_key:
        try:
            explanation = explain_sql_with_gemini(sql, api_key)
        except Exception:
            explanation = explain_sql_fallback(sql)
    else:
        explanation = explain_sql_fallback(sql)
    return jsonify({"explanation": explanation})


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
    api_key = current_user.gemini_key
    result = evaluate_sql_accuracy(sql, natural_query, parsed, api_key if api_key else None)
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
            key     = request.form.get("gemini_key", "").strip()
            existing = User.query.filter_by(email=email).first()
            if existing and existing.id != current_user.id:
                flash("That email is already taken.", "error")
            else:
                current_user.name    = name
                current_user.email   = email
                current_user.company = company
                current_user.role    = role
                current_user.gemini_key = key
                db.session.commit()
                flash("Profile updated successfully!", "success")
        elif action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw     = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if not check_password_hash(current_user.password, current_pw):
                flash("Current password is incorrect.", "error")
            elif new_pw != confirm_pw:
                flash("New passwords do not match.", "error")
            elif len(new_pw) < 6:
                flash("Password must be at least 6 characters.", "error")
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


# ── Static uploads serving ─────────────────────────────────────────────────
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ── Init ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()
    run_migrations()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
