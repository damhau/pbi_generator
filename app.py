"""Flask web application for PBI Generator."""

import json
import logging
import os
import threading
import time
import uuid

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from openai import OpenAI

from models import db, bcrypt, User, UserSettings, DEFAULT_PROMPT
from azdo_client import (
    AzDoClient,
    get_features_from_epic,
    get_epics,
    get_target_iteration_path,
    find_existing_pbi_by_title,
    create_pbi_in_azdo,
    update_pbi_in_azdo,
    validate_parent_feature,
)

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=_log_level)
# Quiet noisy third-party loggers
for _name in ("urllib3", "httpcore", "httpx", "openai"):
    logging.getLogger(_name).setLevel(max(_log_level, logging.WARNING))
logger = logging.getLogger(__name__)

ADMIN_USERNAMES = {"dhauser", "damien"}

# In-memory job store for async PBI generation
_jobs = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 600  # 10 minutes


def _cleanup_old_jobs():
    """Remove job entries older than _JOB_TTL seconds."""
    now = time.time()
    expired = [jid for jid, j in _jobs.items() if now - j["created_at"] > _JOB_TTL]
    for jid in expired:
        del _jobs[jid]
    if expired:
        logger.debug("Cleaned up %d expired jobs", len(expired))


def is_admin():
    return current_user.is_authenticated and current_user.username in ADMIN_USERNAMES


def get_openai_key(settings):
    """Return the effective OpenAI API key (system or personal)."""
    if settings and settings.use_own_openai_key:
        logger.debug("Using personal OpenAI key for user_id=%s", settings.user_id)
        return settings.openai_api_key or ""
    system_key = os.environ.get("SYSTEM_OPENAI_API_KEY", "")
    if system_key:
        logger.debug("Using system OpenAI key for user_id=%s", settings.user_id if settings else "?")
        return system_key
    logger.debug("Falling back to personal OpenAI key for user_id=%s", settings.user_id if settings else "?")
    return settings.openai_api_key if settings else ""


def get_azdo_pat(settings):
    """Return the effective Azure DevOps PAT (system or personal)."""
    if settings and settings.use_own_azdo_pat:
        logger.debug("Using personal AzDO PAT for user_id=%s", settings.user_id)
        return settings.azdo_pat or ""
    system_pat = os.environ.get("SYSTEM_AZDO_PAT", "")
    if system_pat:
        logger.debug("Using system AzDO PAT for user_id=%s", settings.user_id if settings else "?")
        return system_pat
    logger.debug("Falling back to personal AzDO PAT for user_id=%s", settings.user_id if settings else "?")
    return settings.azdo_pat if settings else ""


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///pbi_generator.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)
    bcrypt.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = None
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    with app.app_context():
        db.create_all()
        # Add new columns to existing databases
        with db.engine.connect() as conn:
            from sqlalchemy import inspect, text
            inspector = inspect(db.engine)
            cols = [c["name"] for c in inspector.get_columns("user_settings")]
            if "use_own_openai_key" not in cols:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN use_own_openai_key BOOLEAN DEFAULT 0"))
                logger.info("Added use_own_openai_key column to user_settings")
            if "use_own_azdo_pat" not in cols:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN use_own_azdo_pat BOOLEAN DEFAULT 0"))
                logger.info("Added use_own_azdo_pat column to user_settings")
            conn.commit()

    @app.context_processor
    def inject_admin():
        return {"is_admin": is_admin}

    # ── Auth routes ──────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                logger.info("User '%s' logged in", username)
                return redirect(url_for("index"))
            logger.info("Failed login attempt for username='%s'", username)
            flash("Invalid username or password.", "danger")
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not username or not email or not password:
                flash("All fields are required.", "danger")
                return render_template("register.html")
            if password != confirm:
                flash("Passwords do not match.", "danger")
                return render_template("register.html")
            if User.query.filter((User.username == username) | (User.email == email)).first():
                flash("Username or email already exists.", "danger")
                return render_template("register.html")

            user = User(username=username, email=email)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()
            settings = UserSettings(user_id=user.id, pbi_prompt=DEFAULT_PROMPT)
            db.session.add(settings)
            db.session.commit()
            logger.info("New user registered: '%s' (id=%s)", username, user.id)
            return render_template("register_success.html", username=username)
        return render_template("register.html")

    @app.route("/logout")
    @login_required
    def logout():
        logger.info("User '%s' logged out", current_user.username)
        logout_user()
        return redirect(url_for("login"))

    # ── Main pages ───────────────────────────────────────────────

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    @app.route("/")
    @login_required
    def index():
        return render_template("index.html")

    @app.route("/settings", methods=["GET"])
    @login_required
    def settings_page():
        return render_template("settings.html")

    # ── Admin ────────────────────────────────────────────────────

    @app.route("/admin")
    @login_required
    def admin_page():
        if not is_admin():
            logger.info("Non-admin user '%s' tried to access /admin", current_user.username)
            abort(403)
        return render_template("admin.html")

    @app.route("/api/admin/users", methods=["GET"])
    @login_required
    def admin_list_users():
        if not is_admin():
            return jsonify({"error": "Forbidden"}), 403
        users = User.query.all()
        result = []
        for u in users:
            result.append({
                "id": u.id,
                "username": u.username,
                "email": u.email,
                "has_settings": u.settings is not None,
                "use_system_keys": (
                    not (u.settings.use_own_openai_key if u.settings else True)
                    or not (u.settings.use_own_azdo_pat if u.settings else True)
                ),
            })
        logger.debug("Admin listed %d users", len(result))
        return jsonify(result)

    @app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
    @login_required
    def admin_delete_user(user_id):
        if not is_admin():
            return jsonify({"error": "Forbidden"}), 403
        if user_id == current_user.id:
            return jsonify({"error": "Cannot delete yourself."}), 400
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({"error": "User not found."}), 404
        logger.info("Admin '%s' deleting user '%s' (id=%s)", current_user.username, user.username, user_id)
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "ok"})

    # ── Settings API ─────────────────────────────────────────────

    @app.route("/api/settings", methods=["GET"])
    @login_required
    def get_settings():
        s = current_user.settings
        if not s:
            s = UserSettings(user_id=current_user.id, pbi_prompt=DEFAULT_PROMPT)
            db.session.add(s)
            db.session.commit()
            logger.info("Created default settings for user '%s'", current_user.username)
        data = s.to_dict()
        # Mask secrets for display
        if data["openai_api_key"]:
            data["openai_api_key_masked"] = data["openai_api_key"][:8] + "..." + data["openai_api_key"][-4:]
        else:
            data["openai_api_key_masked"] = ""
        if data["azdo_pat"]:
            data["azdo_pat_masked"] = data["azdo_pat"][:6] + "..." + data["azdo_pat"][-4:]
        else:
            data["azdo_pat_masked"] = ""
        # System key availability
        data["system_openai_available"] = bool(os.environ.get("SYSTEM_OPENAI_API_KEY"))
        data["system_azdo_available"] = bool(os.environ.get("SYSTEM_AZDO_PAT"))
        return jsonify(data)

    @app.route("/api/settings", methods=["PUT"])
    @login_required
    def update_settings():
        s = current_user.settings
        if not s:
            s = UserSettings(user_id=current_user.id)
            db.session.add(s)

        data = request.get_json()
        if "openai_api_key" in data and data["openai_api_key"]:
            s.openai_api_key = data["openai_api_key"]
        if "openai_model" in data:
            s.openai_model = data["openai_model"]
        if "azdo_org_url" in data:
            s.azdo_org_url = data["azdo_org_url"]
        if "azdo_project" in data:
            s.azdo_project = data["azdo_project"]
        if "azdo_team" in data:
            s.azdo_team = data["azdo_team"]
        if "azdo_pat" in data and data["azdo_pat"]:
            s.azdo_pat = data["azdo_pat"]
        if "azdo_area_path" in data:
            s.azdo_area_path = data["azdo_area_path"]
        if "pbi_prompt" in data:
            s.pbi_prompt = data["pbi_prompt"]
        if "use_own_openai_key" in data:
            s.use_own_openai_key = bool(data["use_own_openai_key"])
        if "use_own_azdo_pat" in data:
            s.use_own_azdo_pat = bool(data["use_own_azdo_pat"])

        db.session.commit()
        logger.info("Settings updated for user '%s' (use_own_openai=%s, use_own_azdo=%s)",
                     current_user.username, s.use_own_openai_key, s.use_own_azdo_pat)
        return jsonify({"status": "ok"})

    @app.route("/api/settings/test-azdo", methods=["POST"])
    @login_required
    def test_azdo_connection():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat or not s.azdo_org_url or not s.azdo_project:
            logger.info("AzDO test failed for user '%s': incomplete settings (has_pat=%s, org=%s, project=%s)",
                        current_user.username, bool(pat), bool(s and s.azdo_org_url), bool(s and s.azdo_project))
            return jsonify({"status": "error", "message": "Azure DevOps settings are incomplete."}), 400
        try:
            logger.info("Testing AzDO connection for user '%s' to %s/%s",
                        current_user.username, s.azdo_org_url, s.azdo_project)
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            azdo.get_project_info()
            logger.info("AzDO connection test succeeded for user '%s'", current_user.username)
            return jsonify({"status": "ok", "message": "Connected successfully."})
        except Exception as e:
            logger.exception("AzDO connection test failed for user '%s'", current_user.username)
            return jsonify({"status": "error", "message": str(e)}), 400

    # ── Epic / Feature API ───────────────────────────────────────

    @app.route("/api/epics", methods=["GET"])
    @login_required
    def list_epics():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            logger.info("Epics 400 for user '%s': no settings or no PAT (has_settings=%s, has_pat=%s)",
                        current_user.username, s is not None, bool(pat))
            return jsonify({"error": "Azure DevOps not configured."}), 400
        try:
            logger.debug("Fetching epics for user '%s', area_path='%s'", current_user.username, s.azdo_area_path)
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            epics = get_epics(azdo, s.azdo_area_path)
            logger.info("Fetched %d epics for user '%s'", len(epics), current_user.username)
            return jsonify(epics)
        except Exception as e:
            logger.exception("Epics error for user '%s'", current_user.username)
            return jsonify({"error": str(e)}), 400

    @app.route("/api/features", methods=["GET"])
    @login_required
    def list_features():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            logger.info("Features 400 for user '%s': AzDO not configured", current_user.username)
            return jsonify({"error": "Azure DevOps not configured."}), 400
        epic_title = request.args.get("epic_title", "")
        if not epic_title:
            return jsonify({"error": "epic_title parameter required."}), 400
        try:
            logger.debug("Fetching features for user '%s', epic='%s'", current_user.username, epic_title)
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            features = get_features_from_epic(azdo, epic_title, s.azdo_area_path)
            logger.info("Fetched %d features for epic '%s'", len(features), epic_title)
            return jsonify(features)
        except Exception as e:
            logger.exception("Features error for user '%s', epic='%s'", current_user.username, epic_title)
            return jsonify({"error": str(e)}), 400

    # ── PBI Generation API ───────────────────────────────────────

    def _set_job_stage(job_id, stage):
        with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id]["stage"] = stage

    def _run_generate_job(job_id, openai_key, model, prompt_template, user_request,
                          epic_title, parent_feature_id, pat, azdo_settings, username):
        """Background worker for PBI generation via OpenAI."""
        try:
            # ── Stage 1: Build features context ──
            _set_job_stage(job_id, "fetching_features")
            features_context = ""
            selected_feature_id = "null"
            available_features = []

            if parent_feature_id:
                features_context = f"**PARENT FEATURE OVERRIDE**: Use feature ID {parent_feature_id}."
                selected_feature_id = str(parent_feature_id)
            elif epic_title and pat:
                logger.debug("Job %s: fetching features for epic '%s'", job_id, epic_title)
                azdo = AzDoClient(azdo_settings["org_url"], azdo_settings["project"], pat)
                available_features = get_features_from_epic(azdo, epic_title, azdo_settings["area_path"])
                if available_features:
                    features_context = "**AVAILABLE PARENT FEATURES**:\n"
                    for f in available_features:
                        desc_preview = (f.get("description", "")[:100] + "...") if len(f.get("description", "")) > 100 else f.get("description", "No description")
                        features_context += f"- ID {f['id']}: {f['title']}\n  {desc_preview}\n\n"
                    selected_feature_id = "ID_FROM_LIST_OR_null"

            prompt = prompt_template.format(
                user_request=user_request,
                features_context=features_context,
                selected_feature_id=selected_feature_id,
            )

            # ── Stage 2: Call OpenAI ──
            _set_job_stage(job_id, "calling_ai")
            logger.debug("Job %s: calling OpenAI model=%s for user '%s'", job_id, model, username)
            oai = OpenAI(api_key=openai_key)
            response = oai.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )

            # ── Stage 3: Parse response ──
            _set_job_stage(job_id, "parsing_response")
            content = response.choices[0].message.content.strip()
            logger.debug("Job %s: OpenAI raw response (first 200 chars): %s", job_id, content[:200])

            # Strip markdown code fences
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            pbi_data = json.loads(content)

            # Validate
            for field in ("title", "description", "acceptance_criteria", "priority", "effort"):
                if field not in pbi_data:
                    raise ValueError(f"Missing field: {field}")

            # Normalize parent_feature_id
            pid = pbi_data.get("parent_feature_id")
            if pid and pid != "null":
                try:
                    pbi_data["parent_feature_id"] = int(pid)
                except (ValueError, TypeError):
                    pbi_data["parent_feature_id"] = None
            else:
                pbi_data["parent_feature_id"] = None

            pbi_data.setdefault("tags", ["draft"])

            # Resolve parent feature name
            pf_name = None
            if pbi_data.get("parent_feature_id") and available_features:
                for f in available_features:
                    if f["id"] == pbi_data["parent_feature_id"]:
                        pf_name = f["title"]
                        break
            pbi_data["parent_feature_name"] = pf_name

            logger.info("Job %s: PBI generated for user '%s': title='%s'",
                        job_id, username, pbi_data.get("title", "")[:60])
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["result"] = pbi_data

        except json.JSONDecodeError:
            logger.exception("Job %s: failed to parse AI response as JSON", job_id)
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = "Failed to parse AI response as JSON. Try again."
        except Exception as e:
            logger.exception("Job %s: generate error", job_id)
            with _jobs_lock:
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)

    @app.route("/api/generate", methods=["POST"])
    @login_required
    def generate_pbi():
        s = current_user.settings
        openai_key = get_openai_key(s)
        if not openai_key:
            logger.info("Generate 400 for user '%s': no OpenAI key", current_user.username)
            return jsonify({"error": "OpenAI API key not configured."}), 400

        data = request.get_json()
        user_request = data.get("request", "").strip()
        epic_title = data.get("epic_title", "")
        parent_feature_id = data.get("parent_feature_id")

        if not user_request:
            return jsonify({"error": "Request description is required."}), 400

        logger.info("Generating PBI for user '%s': request='%s', epic='%s'",
                     current_user.username, user_request[:80], epic_title)

        pat = get_azdo_pat(s)
        prompt_template = s.pbi_prompt or DEFAULT_PROMPT
        model = s.openai_model or "gpt-5"
        username = current_user.username
        azdo_settings = {
            "org_url": s.azdo_org_url if s else "",
            "project": s.azdo_project if s else "",
            "area_path": s.azdo_area_path if s else "",
        }

        # Create job and start background thread
        job_id = str(uuid.uuid4())
        with _jobs_lock:
            _cleanup_old_jobs()
            _jobs[job_id] = {
                "status": "pending",
                "stage": "queued",
                "result": None,
                "error": None,
                "created_at": time.time(),
            }

        thread = threading.Thread(
            target=_run_generate_job,
            args=(job_id, openai_key, model, prompt_template, user_request,
                  epic_title, parent_feature_id, pat, azdo_settings, username),
            daemon=True,
        )
        thread.start()

        logger.debug("Job %s started for user '%s'", job_id, username)
        return jsonify({"job_id": job_id}), 202

    @app.route("/api/generate/<job_id>", methods=["GET"])
    @login_required
    def poll_generate(job_id):
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job not found."}), 404
        if job["status"] == "done":
            return jsonify({"status": "done", "result": job["result"]})
        if job["status"] == "error":
            return jsonify({"status": "error", "error": job["error"]})
        return jsonify({"status": "pending", "stage": job.get("stage", "queued")})

    @app.route("/api/create", methods=["POST"])
    @login_required
    def create_pbi():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            logger.info("Create 400 for user '%s': AzDO not configured", current_user.username)
            return jsonify({"error": "Azure DevOps not configured."}), 400

        data = request.get_json()
        pbi_data = data.get("pbi_data")
        next_sprint = data.get("next_sprint", True)
        backlog = data.get("backlog", False)
        update_existing = data.get("update_existing", False)

        if not pbi_data:
            return jsonify({"error": "PBI data is required."}), 400

        logger.info("Creating PBI for user '%s': title='%s', next_sprint=%s, backlog=%s",
                     current_user.username, pbi_data.get("title", "")[:60], next_sprint, backlog)

        try:
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)

            # Validate parent feature
            if pbi_data.get("parent_feature_id"):
                if not validate_parent_feature(azdo, pbi_data["parent_feature_id"]):
                    logger.info("Parent feature %s validation failed, removing link", pbi_data["parent_feature_id"])
                    pbi_data["parent_feature_id"] = None

            # Resolve iteration
            iteration_path = None
            if not backlog:
                iteration_path = get_target_iteration_path(azdo, s.azdo_team, next_sprint)
                logger.debug("Resolved iteration: %s", iteration_path)

            # Check for existing
            existing_id = find_existing_pbi_by_title(azdo, s.azdo_area_path, iteration_path, pbi_data["title"])

            if existing_id and update_existing:
                logger.info("Updating existing PBI #%s", existing_id)
                wi = update_pbi_in_azdo(azdo, existing_id, pbi_data)
                action = "updated"
            elif existing_id:
                logger.info("PBI already exists #%s, not updating", existing_id)
                return jsonify({
                    "error": f"PBI '{pbi_data['title']}' already exists (#{existing_id}). Enable 'Update existing' to overwrite.",
                    "existing_id": existing_id,
                }), 409
            else:
                wi = create_pbi_in_azdo(azdo, pbi_data, s.azdo_area_path, iteration_path)
                action = "created"

            pbi_id = wi.get("id")
            pbi_url = wi.get("_links", {}).get("html", {}).get("href")

            logger.info("PBI #%s %s for user '%s'", pbi_id, action, current_user.username)
            return jsonify({
                "status": "ok",
                "action": action,
                "id": pbi_id,
                "url": pbi_url,
                "iteration": iteration_path,
            })

        except Exception as e:
            logger.exception("Create PBI error for user '%s'", current_user.username)
            return jsonify({"error": str(e)}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
