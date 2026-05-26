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


def run_ocr(image_path: str) -> str:
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = reader.readtext(image_path, detail=0, paragraph=True)
        return "\n".join(results)
    except Exception as exc:
        return f"[OCR error: {exc}]"


def get_gemini_model(api_key: str):
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    return genai.GenerativeModel("gemini-1.5-flash")


def parse_schema_with_gemini(ocr_text: str, api_key: str) -> dict:
    model = get_gemini_model(api_key)
    prompt = f"""You are a database schema expert.
Given the following raw OCR text extracted from a database schema diagram,
identify all tables, their columns (with data types when visible), primary keys,
and foreign key relationships.

Return ONLY a valid JSON object with this exact structure:
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

OCR Text:
{ocr_text}

Return ONLY the JSON, no markdown fences, no explanation."""
    response = model.generate_content(prompt)
    raw = response.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


def generate_sql_with_gemini(natural_query: str, schema: dict, api_key: str) -> str:
    model = get_gemini_model(api_key)
    schema_desc = _build_schema_description(schema)
    prompt = f"""{schema_desc}

User Request: {natural_query}

Generate a single, correct SQL query that satisfies the user's request.
Use standard SQL syntax. Include table aliases where appropriate.
Return ONLY the SQL query, no explanation, no markdown fences."""
    response = model.generate_content(prompt)
    return response.text.strip().lstrip("```sql").lstrip("```").rstrip("```").strip()


def _build_schema_description(schema: dict) -> str:
    schema_desc = "Database Schema:\n"
    for table in schema.get("tables", []):
        cols = ", ".join(
            f"{c['name']} {c.get('type','')}{' PK' if c.get('primary_key') else ''}"
            for c in table["columns"]
        )
        schema_desc += f"  Table {table['name']}: ({cols})\n"
    for rel in schema.get("relationships", []):
        schema_desc += (
            f"  Relationship: {rel['from_table']}.{rel['from_col']} → "
            f"{rel['to_table']}.{rel['to_col']} ({rel.get('type','')})\n"
        )
    return schema_desc


def parse_schema_fallback(ocr_text: str) -> dict:
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    tables = []
    current_table = None
    for line in lines:
        if re.match(r'^[A-Z_][A-Z0-9_]*$', line.upper()) and len(line) < 40:
            if current_table:
                tables.append(current_table)
            current_table = {"name": line, "columns": []}
        elif current_table and re.match(r'^[a-zA-Z_][a-zA-Z0-9_ ]*$', line):
            parts = line.split()
            col_name = parts[0]
            col_type = parts[1] if len(parts) > 1 else "TEXT"
            is_pk = "id" in col_name.lower() and len(current_table["columns"]) == 0
            current_table["columns"].append({
                "name": col_name, "type": col_type,
                "primary_key": is_pk, "nullable": not is_pk
            })
    if current_table:
        tables.append(current_table)
    return {"tables": tables, "relationships": []}


def _fallback_sql(query: str, schema: dict) -> str:
    q = query.lower()
    tables = [t["name"] for t in schema.get("tables", [])]
    first_table = tables[0] if tables else "your_table"
    if "count" in q:
        return f"SELECT COUNT(*) FROM {first_table};"
    if "all" in q or "list" in q or "show" in q or "get" in q:
        return f"SELECT * FROM {first_table} LIMIT 100;"
    if "delete" in q:
        return f"-- DELETE FROM {first_table} WHERE <condition>;"
    if "update" in q or "change" in q:
        return f"UPDATE {first_table} SET <column> = <value> WHERE <condition>;"
    if "insert" in q or "add" in q or "create" in q:
        return f"INSERT INTO {first_table} (<columns>) VALUES (<values>);"
    return f"SELECT * FROM {first_table} WHERE <condition>;"


# ══════════════════════════════════════════════════════════════════
# FEATURE 2: Plain-English SQL Explanation
# ══════════════════════════════════════════════════════════════════
def explain_sql_with_gemini(sql: str, api_key: str) -> str:
    """Convert SQL into a plain English explanation for non-technical users."""
    model = get_gemini_model(api_key)
    prompt = f"""You are a helpful database assistant explaining SQL to a non-technical person.

Given this SQL query:
{sql}

Write a clear, friendly explanation in 2-4 sentences that:
- Describes what data will be retrieved/modified
- Mentions any filters or conditions in plain English
- Avoids technical jargon
- Starts with "This query..."

Return ONLY the explanation text, nothing else."""
    response = model.generate_content(prompt)
    return response.text.strip()


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
    model = get_gemini_model(api_key)
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
    response = model.generate_content(prompt)
    raw = response.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
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
    model = get_gemini_model(api_key)
    schema_desc = _build_schema_description(schema)

    # Build conversation history string
    history_str = ""
    for turn in conversation_history[-6:]:  # last 6 turns max
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

    response = model.generate_content(prompt)
    return response.text.strip().lstrip("```sql").lstrip("```").rstrip("```").strip()


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
            parsed = parse_schema_with_gemini(ocr_text, api_key)
        except Exception as exc:
            parsed = parse_schema_fallback(ocr_text)
            flash(f"AI parsing failed ({exc}); used heuristic parser instead.", "warning")
    else:
        parsed = parse_schema_fallback(ocr_text)
        flash("No Gemini API key — used heuristic parser. Add your key in Settings for better results.", "info")
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
            model_used = "gemini-1.5-flash"
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
        model_used="gemini-1.5-flash",
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