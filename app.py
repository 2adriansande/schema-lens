import os
import json
import re
import datetime
from pathlib import Path
import google.generativeai as genai

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_from_directory)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                          login_required, current_user)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

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
    created_at     = db.Column(db.DateTime, default=datetime.datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ── Helpers ────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def run_ocr(image_path: str) -> str:
    """Extract text from an image using EasyOCR."""
    try:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = reader.readtext(image_path, detail=0, paragraph=True)
        return "\n".join(results)
    except Exception as exc:
        return f"[OCR error: {exc}]"


def parse_schema_with_gemini(ocr_text: str, api_key: str) -> dict:
    """Use Gemini to turn raw OCR text into a structured schema dict."""
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

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
    """Generate a SQL query from natural language given a parsed schema."""
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")

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

    prompt = f"""{schema_desc}

User Request: {natural_query}

Generate a single, correct SQL query that satisfies the user's request.
Use standard SQL syntax. Include table aliases where appropriate.
Return ONLY the SQL query, no explanation, no markdown fences."""

    response = model.generate_content(prompt)
    return response.text.strip().lstrip("```sql").lstrip("```").rstrip("```").strip()


def parse_schema_fallback(ocr_text: str) -> dict:
    """Heuristic parser when no API key is available."""
    lines = [l.strip() for l in ocr_text.splitlines() if l.strip()]
    tables = []
    current_table = None
    for line in lines:
        upper = line.upper()
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


# ── Auth Routes ────────────────────────────────────────────────────────────
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

        user = User(
            name=name, email=email,
            password=generate_password_hash(password, method="scrypt")
        )
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


# ── Dashboard ──────────────────────────────────────────────────────────────
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


# ── Schema Upload & Management ─────────────────────────────────────────────
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

    filename = secure_filename(f"user{current_user.id}_{int(datetime.datetime.utcnow().timestamp())}_{file.filename}")
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    # OCR
    ocr_text = run_ocr(filepath)

    # Schema parsing
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
        flash("No Gemini API key set — used heuristic schema parser. Add your key in Settings for better results.", "info")

    schema = SchemaUpload(
        user_id=current_user.id,
        filename=filename,
        original_name=file.filename,
        schema_name=schema_name,
        ocr_text=ocr_text,
        parsed_schema=json.dumps(parsed)
    )
    db.session.add(schema)
    db.session.commit()
    flash("Schema uploaded and processed successfully!", "success")
    return redirect(url_for("schema_detail", schema_id=schema.id))


@app.route("/schemas/<int:schema_id>")
@login_required
def schema_detail(schema_id):
    schema = SchemaUpload.query.filter_by(id=schema_id, user_id=current_user.id).first_or_404()
    parsed = json.loads(schema.parsed_schema) if schema.parsed_schema else {}
    queries = QueryHistory.query.filter_by(schema_id=schema_id)\
              .order_by(QueryHistory.created_at.desc()).all()
    return render_template("schema_detail.html", schema=schema,
                           parsed=parsed, queries=queries)


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


# ── Query Generation ───────────────────────────────────────────────────────
@app.route("/query", methods=["GET", "POST"])
@login_required
def query_page():
    schemas = SchemaUpload.query.filter_by(user_id=current_user.id)\
              .order_by(SchemaUpload.uploaded_at.desc()).all()
    selected_schema = None
    preselect_id = request.args.get("schema_id")
    if preselect_id:
        selected_schema = SchemaUpload.query.filter_by(
            id=preselect_id, user_id=current_user.id).first()
    return render_template("query.html", schemas=schemas,
                           selected_schema=selected_schema)


@app.route("/api/generate-sql", methods=["POST"])
@login_required
def generate_sql():
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
        if schema_obj and schema_obj.parsed_schema:
            parsed = json.loads(schema_obj.parsed_schema)

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
        # Fallback: template-based generation
        sql = _fallback_sql(natural_query, parsed)
        model_used = "heuristic"

    history = QueryHistory(
        user_id=current_user.id,
        schema_id=schema_obj.id if schema_obj else None,
        natural_query=natural_query,
        generated_sql=sql,
        model_used=model_used
    )
    db.session.add(history)
    db.session.commit()

    return jsonify({"sql": sql, "model": model_used, "query_id": history.id})


def _fallback_sql(query: str, schema: dict) -> str:
    """Very basic heuristic SQL generator."""
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


# ── Query History ──────────────────────────────────────────────────────────
@app.route("/history")
@login_required
def history():
    page    = request.args.get("page", 1, type=int)
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


# ── Settings ───────────────────────────────────────────────────────────────
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


# ── Data Normalization ─────────────────────────────────────────────────────
@app.route("/normalize")
@login_required
def normalize():
    schemas = SchemaUpload.query.filter_by(user_id=current_user.id)\
              .order_by(SchemaUpload.uploaded_at.desc()).all()
    return render_template("normalize.html", schemas=schemas)


@app.route('/api/normalize-schema', methods=['POST'])
@login_required
def normalize_schema_api():
    data = request.get_json()
    raw_input = (data.get("input") or "").strip()

    if not raw_input:
        return jsonify({"error": "Input cannot be empty."}), 400

    api_key = current_user.gemini_key

    if not api_key:
        return jsonify({"error": "Gemini API key not configured."}), 400

    result = _normalize_schema_gemini(raw_input, api_key)

    return jsonify({
        "result": result,
        "method": "gemini-1.5-flash"
    })


@app.route("/api/normalize-data", methods=["POST"])
@login_required
def normalize_data_api():
    """Option B: Data cleaner — standardize dirty/random data."""
    data = request.get_json()
    raw_input = (data.get("input") or "").strip()
    if not raw_input:
        return jsonify({"error": "Input cannot be empty."}), 400

    api_key = current_user.gemini_key
    if api_key:
        try:
            result = _normalize_data_gemini(raw_input, api_key)
            return jsonify({"result": result, "method": "gemini-1.5-flash"})
        except Exception as exc:
            return jsonify({"error": f"Gemini error: {exc}"}), 500
    else:
        result = _normalize_data_heuristic(raw_input)
        return jsonify({"result": result, "method": "heuristic"})


# ── DDL Generation (on demand) ─────────────────────────────────────────────
@app.route('/api/generate-ddl', methods=['POST'])
@login_required
def generate_ddl_api():
    """Generate SQL DDL (CREATE TABLE) from 3NF tables — only called when user requests it."""
    data = request.get_json()
    tables = data.get("tables", [])

    if not tables:
        return jsonify({"error": "No tables provided."}), 400

    api_key = current_user.gemini_key
    if api_key:
        try:
            result = _generate_ddl_gemini(tables, api_key)
            return jsonify({"ddl": result, "method": "gemini-1.5-flash"})
        except Exception:
            pass  # Fall through to heuristic

    # Heuristic DDL generation
    ddl_parts = []
    for t in tables:
        tname = t.get("name", "table")
        cols = t.get("columns", [])
        col_lines = []
        fk_lines = []
        pk_names = []
        for c in cols:
            cname = c.get("name", "col") if isinstance(c, dict) else str(c)
            ctype = c.get("type", "TEXT") if isinstance(c, dict) else "TEXT"
            nullable = c.get("nullable", True) if isinstance(c, dict) else True
            is_pk = c.get("primary_key", False) if isinstance(c, dict) else False
            fk = c.get("foreign_key", "") if isinstance(c, dict) else ""
            line = f"  {cname} {ctype}"
            if not nullable:
                line += " NOT NULL"
            col_lines.append(line)
            if is_pk:
                pk_names.append(cname)
            if fk and "." in fk:
                ref_t, ref_c = fk.split(".", 1)
                fk_lines.append(f"  FOREIGN KEY ({cname}) REFERENCES {ref_t}({ref_c})")
        if pk_names:
            col_lines.append(f"  PRIMARY KEY ({', '.join(pk_names)})")
        all_lines = col_lines + fk_lines
        ddl_parts.append(f"CREATE TABLE {tname} (\n" + ",\n".join(all_lines) + "\n);")
    return jsonify({"ddl": "\n\n".join(ddl_parts), "method": "heuristic"})


def _generate_ddl_gemini(tables: list, api_key: str) -> str:
    """Generate complete SQL DDL from 3NF table definitions using Gemini."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    tables_desc = json.dumps(tables, indent=2)
    prompt = f"""Given these 3NF normalized table definitions, generate complete SQL DDL.

Tables:
{tables_desc}

Generate CREATE TABLE statements with:
- Proper SQL data types (INT, VARCHAR, TEXT, DATE, DECIMAL, BOOLEAN)
- PRIMARY KEY constraints
- FOREIGN KEY constraints with REFERENCES
- NOT NULL on required fields
- Appropriate UNIQUE constraints where logical

Return ONLY the SQL statements, no markdown fences, no explanation."""
    response = model.generate_content(prompt)
    sql = response.text.strip()
    sql = re.sub(r"^```sql\s*", "", sql)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip()


def _normalize_schema_gemini(raw_input: str, api_key: str) -> dict:
    """Perform rigorous step-by-step normalization (UNF→1NF→2NF→3NF) using Gemini."""
    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config={
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 40,
            "max_output_tokens": 8192,
            "response_mime_type": "application/json"
        }
    )

    prompt = f"""You are a senior database normalization expert.
Perform rigorous step-by-step normalization from UNF to 3NF.

STRICT RULES:
- Return ONLY valid JSON. No markdown fences, no explanation outside JSON.
- NEVER skip normalization stages (UNF then 1NF then 2NF then 3NF).
- NEVER invent data not present in the source dataset.
- State all assumptions explicitly in explanation fields.
- Explain EVERY normalization decision in detail.
- Identify ALL functional, partial, and transitive dependencies.

Dataset:
{raw_input}

Return this EXACT JSON structure (fill every field):

{{
  "unf": {{
    "description": "Plain-English description of the original unnormalized structure",
    "candidate_keys": ["candidate key set 1", "candidate key set 2"],
    "primary_key": "chosen primary key (or col1, col2 for composite)",
    "repeating_groups": ["repeating group or multi-valued attribute found"],
    "multivalued_attributes": ["attribute containing multiple values per cell"],
    "functional_dependencies": [
      {{"determinant": "A", "dependent": "B, C", "description": "A uniquely determines B and C"}}
    ],
    "original_issues": ["normalization violation 1", "normalization violation 2"]
  }},
  "1nf": {{
    "explanation": "Detailed explanation of what was done to achieve 1NF — atomicity, unique rows, single data type per column",
    "changes_made": ["specific change 1", "specific change 2"],
    "functional_dependencies": [
      {{"determinant": "A", "dependent": "B, C"}}
    ],
    "table": {{
      "name": "table_name_1nf",
      "columns": ["col1", "col2", "col3"],
      "primary_key": "col1",
      "sample_rows": [
        {{"col1": "val1", "col2": "val2"}}
      ]
    }}
  }},
  "2nf": {{
    "explanation": "Detailed explanation of 2NF — partial dependencies identified and removed",
    "partial_dependencies_removed": [
      {{
        "attribute": "attribute_name",
        "depends_on": "part_of_composite_key",
        "full_key": "full composite key",
        "moved_to_table": "new_table_name",
        "reason": "why this is a partial dependency"
      }}
    ],
    "tables": [
      {{
        "name": "table_name",
        "primary_key": "pk_col",
        "foreign_keys": ["fk_col to other_table(pk)"],
        "columns": ["col1", "col2"]
      }}
    ]
  }},
  "3nf": {{
    "explanation": "Detailed explanation of 3NF — transitive dependencies identified and removed",
    "transitive_dependencies_removed": [
      {{
        "non_key_attribute": "attribute_A",
        "determines": "attribute_B",
        "via": "through which non-key attribute",
        "moved_to_table": "new_table_name",
        "reason": "why this is a transitive dependency"
      }}
    ],
    "tables": [
      {{
        "name": "table_name",
        "primary_key": "pk_col",
        "foreign_keys": ["fk_col to other_table(pk)"],
        "columns": [
          {{"name": "col", "type": "TEXT", "primary_key": false, "nullable": true, "foreign_key": ""}}
        ]
      }}
    ]
  }},
  "validation": {{
    "1nf": {{"satisfied": true, "reason": "All cells atomic, rows uniquely identifiable, single data type per column"}},
    "2nf": {{"satisfied": true, "reason": "No partial dependencies exist — all non-key attributes depend on the whole primary key"}},
    "3nf": {{"satisfied": true, "reason": "No transitive dependencies — every non-key attribute depends only on the primary key"}}
  }},
  "relationships": [
    {{"from_table": "t1", "from_col": "c1", "to_table": "t2", "to_col": "c2", "type": "one-to-many"}}
  ],
  "normalized_data": {{
    "table_name": [{{"col1": "val1"}}]
  }},
  "sql_inserts": "INSERT INTO ... statements as one string"
}}
"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"^```\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        result = json.loads(text)

        # Ensure all required top-level keys with safe defaults
        defaults = {
            "unf": {"description": "", "candidate_keys": [], "primary_key": "",
                    "repeating_groups": [], "multivalued_attributes": [],
                    "functional_dependencies": [], "original_issues": []},
            "1nf": {"explanation": "", "changes_made": [], "functional_dependencies": [],
                    "table": {"name": "", "columns": [], "primary_key": "", "sample_rows": []}},
            "2nf": {"explanation": "", "partial_dependencies_removed": [], "tables": []},
            "3nf": {"explanation": "", "transitive_dependencies_removed": [], "tables": []},
            "validation": {
                "1nf": {"satisfied": True, "reason": ""},
                "2nf": {"satisfied": True, "reason": ""},
                "3nf": {"satisfied": True, "reason": ""}
            },
            "relationships": [],
            "normalized_data": {},
            "sql_inserts": ""
        }
        for key, default_val in defaults.items():
            if key not in result:
                result[key] = default_val

        # Supply legacy keys for backward-compat with any existing callers
        result["analysis"] = {
            "original_issues": result.get("unf", {}).get("original_issues", []),
            "normal_forms_applied": ["UNF", "1NF", "2NF", "3NF"]
        }
        result["tables"] = result.get("3nf", {}).get("tables", [])
        result["ddl"] = ""  # DDL generated on-demand via /api/generate-ddl

        return result

    except json.JSONDecodeError as e:
        return _normalize_schema_error(f"Gemini returned invalid JSON: {str(e)}")
    except Exception as e:
        return _normalize_schema_error(f"Normalization failed: {str(e)}")


def _normalize_schema_error(msg: str) -> dict:
    """Return a safe error-state structure for the schema normalizer."""
    empty_table = {"name": "", "columns": [], "primary_key": "", "sample_rows": []}
    return {
        "unf": {"description": msg, "candidate_keys": [], "primary_key": "",
                "repeating_groups": [], "multivalued_attributes": [],
                "functional_dependencies": [], "original_issues": [msg]},
        "1nf": {"explanation": "", "changes_made": [], "functional_dependencies": [], "table": empty_table},
        "2nf": {"explanation": "", "partial_dependencies_removed": [], "tables": []},
        "3nf": {"explanation": "", "transitive_dependencies_removed": [], "tables": []},
        "validation": {
            "1nf": {"satisfied": False, "reason": msg},
            "2nf": {"satisfied": False, "reason": msg},
            "3nf": {"satisfied": False, "reason": msg}
        },
        "relationships": [], "normalized_data": {}, "sql_inserts": "",
        "analysis": {"original_issues": [msg], "normal_forms_applied": []},
        "tables": [], "ddl": ""
    }


def _normalize_schema_heuristic(raw_input: str) -> dict:
    """Basic heuristic schema normalizer and data splitter when no API key is available."""
    import csv
    from io import StringIO

    def detect_delimiter(sample: str) -> str:
        for delim in [",", "\t", "|", ";"]:
            if sample.count(delim) > 1:
                return delim
        return ","

    def build_stage_table(name: str, rows: list[dict], columns: list[dict]) -> dict:
        headers = [c["name"] for c in columns]
        preview_rows = [[row.get(h, "") for h in headers] for row in rows]
        return {"name": name, "headers": headers, "rows": preview_rows}

    lines = [l for l in raw_input.splitlines() if l.strip()]
    if not lines:
        return {
            "analysis": {"original_issues": [], "normal_forms_applied": []},
            "tables": [], "relationships": [], "ddl": "", "normalized_data": {}, "sql_inserts": "", "stages": []
        }

    delim = detect_delimiter(lines[0])
    try:
        f = StringIO(raw_input)
        reader = csv.reader(f, delimiter=delim, skipinitialspace=True)
        all_rows = [row for row in reader if any(cell.strip() for cell in row)]
    except Exception:
        all_rows = [[c.strip() for c in l.split(delim)] for l in lines]

    if not all_rows:
        return {
            "analysis": {"original_issues": [], "normal_forms_applied": []},
            "tables": [], "relationships": [], "ddl": "", "normalized_data": {}, "sql_inserts": "", "stages": []
        }

    headers = [h.strip() for h in all_rows[0]]
    data_rows = all_rows[1:]

    clean_headers = []
    for h in headers:
        ch = h.lower().replace(" ", "_")
        ch = re.sub(r"[^a-z0-9_]", "", ch)
        if not ch:
            ch = f"col_{len(clean_headers)}"
        clean_headers.append(ch)

    first_col = clean_headers[0] if clean_headers else ""
    first_parts = first_col.split("_")
    main_prefix = first_parts[0] if len(first_parts) > 1 and first_parts[0] not in ("id", "pk", "key") else ""

    if main_prefix:
        main_table = main_prefix + "s"
        if main_table.endswith("ss"):
            main_table = main_prefix + "es"
    else:
        main_table = "main_data"

    prefix_to_cols = {}
    for col in clean_headers:
        parts = col.split("_")
        if len(parts) > 1:
            pfx = parts[0]
            prefix_to_cols.setdefault(pfx, []).append(col)

    lookup_tables_info = {}
    for pfx, cols in prefix_to_cols.items():
        if pfx != main_prefix and len(cols) >= 2 and pfx not in ("id", "date", "qty", "quantity", "is", "has", "col"):
            tname = pfx + "s"
            if tname.endswith("ss"):
                tname = pfx + "es"

            tcols = [{"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False, "foreign_key": ""}]
            orig_cols = []
            for col in cols:
                rest = col[len(pfx):].lstrip("_")
                cname = rest if rest else "name"
                tcols.append({"name": cname, "type": "TEXT", "primary_key": False, "nullable": True, "foreign_key": ""})
                orig_cols.append(col)
            lookup_tables_info[tname] = {
                "prefix": pfx,
                "columns": tcols,
                "orig_cols": orig_cols,
                "data": [],
                "seen": {}
            }

    main_columns = []
    first_is_pk = False
    if clean_headers:
        fc = clean_headers[0]
        if fc.endswith("_id") or fc == "id" or fc == "pk":
            first_is_pk = True

    lookup_orig_cols = [c for info in lookup_tables_info.values() for c in info["orig_cols"]]

    for idx, col in enumerate(clean_headers):
        if col in lookup_orig_cols:
            continue
        is_pk = idx == 0 and first_is_pk
        main_columns.append({
            "name": col,
            "type": "INTEGER" if is_pk or col.endswith("_id") else "TEXT",
            "primary_key": is_pk,
            "nullable": not is_pk,
            "foreign_key": ""
        })

    for tname, info in lookup_tables_info.items():
        fk_col = f"{info['prefix']}_id"
        main_columns.append({
            "name": fk_col,
            "type": "INTEGER",
            "primary_key": False,
            "nullable": True,
            "foreign_key": f"{tname}.id"
        })

    if not any(c["primary_key"] for c in main_columns):
        main_columns.insert(0, {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False, "foreign_key": ""})

    main_table_data = []
    stage1_rows = []
    for row_idx, row in enumerate(data_rows, 1):
        row_vals = [row[h_idx].strip() if h_idx < len(row) else "" for h_idx in range(len(clean_headers))]
        stage1_rows.append(row_vals)

        row_dict = {clean_headers[h_idx]: row_vals[h_idx] for h_idx in range(len(clean_headers))}

        fk_values = {}
        for tname, info in lookup_tables_info.items():
            vals = tuple(row_dict.get(c, "") for c in info["orig_cols"])
            if not any(vals):
                fk_values[tname] = None
                continue
            if vals not in info["seen"]:
                new_id = len(info["seen"]) + 1
                info["seen"][vals] = new_id
                lookup_row = {"id": new_id}
                for c in info["orig_cols"]:
                    rest = c[len(info["prefix"]):].lstrip("_")
                    cname = rest if rest else "name"
                    lookup_row[cname] = row_dict.get(c, "")
                info["data"].append(lookup_row)
            fk_values[tname] = info["seen"][vals]

        main_row = {}
        for c in main_columns:
            cname = c["name"]
            if c["primary_key"] and cname not in clean_headers:
                main_row[cname] = row_idx
            elif c["foreign_key"]:
                t_ref = c["foreign_key"].split(".")[0]
                main_row[cname] = fk_values.get(t_ref)
            else:
                main_row[cname] = row_dict.get(cname, "")
        main_table_data.append(main_row)

    tables = [{"name": main_table, "columns": main_columns}]
    for tname, info in lookup_tables_info.items():
        tables.append({"name": tname, "columns": info["columns"]})

    relationships = []
    for col in main_columns:
        if col["foreign_key"]:
            ref_parts = col["foreign_key"].split(".")
            relationships.append({
                "from_table": main_table,
                "from_col": col["name"],
                "to_table": ref_parts[0],
                "to_col": ref_parts[1],
                "type": "many-to-one"
            })

    ddl_parts = []
    for t in tables:
        cols_sql = []
        pks = []
        for c in t["columns"]:
            line = f"  {c['name']} {c['type']}"
            if not c["nullable"]:
                line += " NOT NULL"
            cols_sql.append(line)
            if c["primary_key"]:
                pks.append(c["name"])
        if pks:
            cols_sql.append(f"  PRIMARY KEY ({', '.join(pks)})")
        ddl_parts.append(f"CREATE TABLE {t['name']} (\n" + ",\n".join(cols_sql) + "\n);")

    normalized_data = {main_table: main_table_data}
    for tname, info in lookup_tables_info.items():
        normalized_data[tname] = info["data"]

    inserts_list = []
    for tname in lookup_tables_info.keys():
        tinfo = lookup_tables_info[tname]
        for r in tinfo["data"]:
            cols = list(r.keys())
            vals = ["'" + str(v).replace("'", "''") + "'" if isinstance(v, str) else str(v) if v is not None else "NULL" for v in r.values()]
            inserts_list.append(f"INSERT INTO {tname} ({', '.join(cols)}) VALUES ({', '.join(vals)});")
    for r in main_table_data:
        cols = list(r.keys())
        vals = ["'" + str(v).replace("'", "''") + "'" if isinstance(v, str) else str(v) if v is not None else "NULL" for v in r.values()]
        inserts_list.append(f"INSERT INTO {main_table} ({', '.join(cols)}) VALUES ({', '.join(vals)});")

    stage2_tables = [build_stage_table(main_table, main_table_data, main_columns)]
    for tname, info in lookup_tables_info.items():
        stage2_tables.append(build_stage_table(tname, info["data"], info["columns"]))

    stage3_tables = stage2_tables.copy()

    return {
        "analysis": {
            "original_issues": ["Flat structure detected — separated lookup tables for foreign keys"],
            "normal_forms_applied": ["1NF", "2NF", "3NF"]
        },
        "tables": tables,
        "relationships": relationships,
        "ddl": "\n\n".join(ddl_parts),
        "normalized_data": normalized_data,
        "sql_inserts": "\n".join(inserts_list),
        "stages": [
            {"name": "1NF", "description": "Flat input table with atomic values", "tables": [{"name": main_table + "_raw", "headers": clean_headers, "rows": stage1_rows}]},
            {"name": "2NF", "description": "Split repeating groups into separate lookup tables", "tables": stage2_tables},
            {"name": "3NF", "description": "Final normalized tables with inferred foreign keys", "tables": stage3_tables}
        ]
    }


def _normalize_data_gemini(raw_input: str, api_key: str) -> dict:
    """Use Gemini to clean and standardize dirty/random data — with full change tracking."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-2.5-flash",
        generation_config={"temperature": 0.1, "response_mime_type": "application/json"}
    )

    prompt = f"""You are a professional data cleaning and standardization expert.

Clean and standardize the following raw dataset. Apply ALL these rules:
1. Fix inconsistent capitalization: Title Case for names/cities, UPPER for codes/SKUs, lowercase for emails
2. Standardize ALL date formats to YYYY-MM-DD
3. Remove extra whitespace (leading, trailing, consecutive spaces)
4. Standardize phone numbers to XXX-XXX-XXXX format where possible
5. Standardize email addresses to lowercase
6. Strip currency symbols from numeric fields, standardize decimal format
7. Remove exact duplicate rows
8. Detect missing values (empty cells, "N/A", "null", "?", "-", "none", "unknown")
9. Flag suspicious values (future dates, negative prices, malformed emails, etc.)
10. NEVER fabricate missing data — leave genuinely missing values as empty strings

Return ONLY valid JSON with this structure:
{{
  "summary": {{
    "rows_processed": 0,
    "duplicates_removed": 0,
    "formatting_corrections": 0,
    "missing_values_detected": 0,
    "suspicious_values_flagged": 0,
    "issues_found": ["issue 1"],
    "changes_made": ["Row N, col_name: 'old' to 'new'"]
  }},
  "headers": ["col1", "col2"],
  "original_data": [["r1c1", "r1c2"]],
  "cleaned_data": [["r1c1_clean", "r1c2_clean"]],
  "changed_cells": [
    {{"row": 0, "col": 0, "original": "old_val", "cleaned": "new_val"}}
  ],
  "missing_values": [
    {{"row": 1, "col": 2, "column_name": "email", "description": "Cell is empty"}}
  ],
  "suspicious_values": [
    {{"row": 2, "col": 3, "column_name": "date", "value": "2099-01-01", "reason": "Date is in the far future"}}
  ],
  "sql_inserts": "INSERT INTO cleaned_data (...) VALUES (...);"
}}

Raw Input:
{raw_input}

Return ONLY the JSON object, no markdown fences, no explanation outside the JSON."""

    response = model.generate_content(prompt)
    raw = response.text.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    result = json.loads(raw.strip())

    # Ensure required keys with safe defaults
    for key, default in [
        ("summary", {"rows_processed": 0, "duplicates_removed": 0, "formatting_corrections": 0,
                     "missing_values_detected": 0, "suspicious_values_flagged": 0,
                     "issues_found": [], "changes_made": []}),
        ("headers", []), ("original_data", []), ("cleaned_data", []),
        ("changed_cells", []), ("missing_values", []), ("suspicious_values", []),
        ("sql_inserts", "")
    ]:
        if key not in result:
            result[key] = default

    return result


def _normalize_data_heuristic(raw_input: str) -> dict:
    """Enhanced heuristic data cleaner — detects missing and suspicious values."""
    import csv
    from io import StringIO

    def detect_delimiter(header_line: str) -> str:
        candidates = [",", "\t", "|", ";"]
        return max(candidates, key=lambda d: header_line.count(d))

    def normalize_value(value: str, header: str) -> str:
        text = " ".join(value.strip().split())
        lower_header = header.lower()

        if not text:
            return ""

        if "email" in lower_header:
            return text.lower()

        if any(token in lower_header for token in ["phone", "tel", "contact"]):
            digits = re.sub(r"\D", "", text)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) == 10:
                return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
            if len(digits) == 7:
                return f"{digits[0:3]}-{digits[3:7]}"
            return text

        if any(token in lower_header for token in ["date", "dob", "joined", "signup", "created", "updated"]):
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%m.%d.%Y", "%d.%m.%Y", "%m/%d/%y", "%d/%m/%y", "%m-%d-%y", "%d-%m-%y", "%m.%d.%y", "%d.%m.%y"]:
                try:
                    date = datetime.datetime.strptime(text, fmt)
                    return date.strftime("%Y-%m-%d")
                except ValueError:
                    continue
            match = re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$', text)
            if match:
                p1, p2, year = match.groups()
                if len(year) == 2:
                    year = "20" + year
                if int(p1) > 12 and int(p2) <= 12:
                    return f"{year}-{p2.zfill(2)}-{p1.zfill(2)}"
                return f"{year}-{p1.zfill(2)}-{p2.zfill(2)}"
            return text

        if any(token in lower_header for token in ["amount", "price", "total", "cost", "spent", "value"]):
            cleaned = re.sub(r"[$,€£¥₱ ]", "", text)
            return cleaned

        if any(token in lower_header for token in ["code", "sku", "id", "region", "status", "type"]):
            return text.upper()

        if any(token in lower_header for token in ["name", "city", "state", "country", "address", "street"]):
            return text.title()

        if text.isupper() and len(text.split()) <= 3:
            return text.title()

        return text

    def is_missing(val: str) -> bool:
        return val.strip().lower() in ("", "n/a", "na", "null", "none", "?", "-", "--", "unknown")

    def is_suspicious(val: str, header: str):
        lower_h = header.lower()
        if any(t in lower_h for t in ["date", "dob", "joined", "signup"]):
            for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"]:
                try:
                    d = datetime.datetime.strptime(val, fmt)
                    if d > datetime.datetime.now():
                        return "Date is in the future"
                    if d.year < 1900:
                        return "Date is suspiciously old (before 1900)"
                except ValueError:
                    pass
        if any(t in lower_h for t in ["amount", "price", "total", "cost"]):
            cleaned_num = re.sub(r"[$,€£¥₱ ]", "", val)
            try:
                if float(cleaned_num) < 0:
                    return "Negative value in a price/amount field"
            except ValueError:
                pass
        if any(t in lower_h for t in ["date", "dob"]):
            try:
                d = datetime.datetime.strptime(val, "%Y-%m-%d")
                if d > datetime.datetime.now(): return "Date is in the future"
            except ValueError: pass
        if any(t in lower_h for t in ["amount", "price"]):
            try:
                if float(re.sub(r"[$,€£¥₱ ]", "", val)) < 0: return "Negative value"
            except ValueError: pass
        return None

    lines = [line for line in raw_input.splitlines() if line.strip()]
    if not lines: return {"summary": {}, "headers": [], "original_data": [], "cleaned_data": [], "changed_cells": [], "missing_values": [], "suspicious_values": [], "sql_inserts": ""}

    delim = detect_delimiter(lines[0])
    f = StringIO(raw_input)
    reader = csv.reader(f, delimiter=delim, skipinitialspace=True)
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]
    headers = [h.strip().lower().replace(" ", "_") for h in all_rows[0] if h.strip()]
    cleaned_data = []
    original_data = []
    changes = []
    changed_cells = []
    missing_values = []
    suspicious_values = []
    seen = set()
    formatting_corrections = 0

    for idx, row in enumerate(all_rows[1:], 1):
        cells = [cell.strip() for cell in row]
        if len(cells) < len(headers): cells += [""] * (len(headers) - len(cells))
        else: cells = cells[:len(headers)]
        original_data.append(list(cells))
        normalized_row = []
        for j, cell in enumerate(cells):
            header = headers[j] if j < len(headers) else f"col_{j}"
            if is_missing(cell):
                missing_values.append({"row": idx, "col": j, "column_name": header, "description": f"Missing value: '{cell}'" if cell.strip() else "Empty cell"})
            normalized = normalize_value(cell, header)
            susp_reason = is_suspicious(normalized or cell, header)
            if susp_reason and not is_missing(cell):
                suspicious_values.append({"row": idx, "col": j, "column_name": header, "value": normalized or cell, "reason": susp_reason})
            if normalized != cell and cell.strip():
                changes.append(f"Row {idx}, {header}: '{cell}' → '{normalized}'")
                changed_cells.append({"row": idx - 1, "col": j, "original": cell, "cleaned": normalized})
                formatting_corrections += 1
            normalized_row.append(normalized)
        row_key = tuple(normalized_row)
        if row_key in seen:
            continue
        seen.add(row_key)
        cleaned_data.append(normalized_row)

    table_name = "cleaned_data"
    inserts = []
    for row in cleaned_data:
        vals = ", ".join("'" + str(val).replace("'", "''") + "'" for val in row)
        inserts.append(f"INSERT INTO {table_name} ({', '.join(headers)}) VALUES ({vals});")

    issues = []
    if changes:
        issues.append(f"{len(changes)} changes applied")
    if len(original_data) != len(cleaned_data):
        issues.append(f"{len(original_data) - len(cleaned_data)} duplicate rows removed")

    return {
        "summary": {
            "rows_processed": len(cleaned_data),
            "issues_found": issues,
            "changes_made": changes[:20]
        },
        "headers": headers,
        "original_data": original_data,
        "cleaned_data": cleaned_data,
        "sql_inserts": "\n".join(inserts)
    }


# ── Static uploads serving ─────────────────────────────────────────────────
@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ── Init ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)


# ── Jinja Filters ──────────────────────────────────────────────────────────
@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}