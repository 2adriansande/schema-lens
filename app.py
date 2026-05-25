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
    model = genai.GenerativeModel("gemini-1.5-flash")

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
    model = genai.GenerativeModel("gemini-1.5-flash")

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
