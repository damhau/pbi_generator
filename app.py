"""Flask web application for PBI Generator."""

import json
import logging
import os

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_USERNAMES = {"dhauser", "damien"}


def is_admin():
    return current_user.is_authenticated and current_user.username in ADMIN_USERNAMES


def get_openai_key(settings):
    """Return the effective OpenAI API key (system or personal)."""
    if settings and settings.use_own_openai_key:
        return settings.openai_api_key or ""
    return os.environ.get("SYSTEM_OPENAI_API_KEY", "") or (settings.openai_api_key if settings else "")


def get_azdo_pat(settings):
    """Return the effective Azure DevOps PAT (system or personal)."""
    if settings and settings.use_own_azdo_pat:
        return settings.azdo_pat or ""
    return os.environ.get("SYSTEM_AZDO_PAT", "") or (settings.azdo_pat if settings else "")


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
            if "use_own_azdo_pat" not in cols:
                conn.execute(text("ALTER TABLE user_settings ADD COLUMN use_own_azdo_pat BOOLEAN DEFAULT 0"))
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
                return redirect(url_for("index"))
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
            return render_template("register_success.html", username=username)
        return render_template("register.html")

    @app.route("/logout")
    @login_required
    def logout():
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
        return jsonify({"status": "ok"})

    @app.route("/api/settings/test-azdo", methods=["POST"])
    @login_required
    def test_azdo_connection():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat or not s.azdo_org_url or not s.azdo_project:
            return jsonify({"status": "error", "message": "Azure DevOps settings are incomplete."}), 400
        try:
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            azdo.get_project_info()
            return jsonify({"status": "ok", "message": "Connected successfully."})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 400

    # ── Epic / Feature API ───────────────────────────────────────

    @app.route("/api/epics", methods=["GET"])
    @login_required
    def list_epics():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            logger.info("Epics 400: no settings or no PAT. has_settings=%s, has_pat=%s",
                        s is not None, bool(pat))
            return jsonify({"error": "Azure DevOps not configured."}), 400
        try:
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            epics = get_epics(azdo, s.azdo_area_path)
            return jsonify(epics)
        except Exception as e:
            logger.exception("Epics error")
            return jsonify({"error": str(e)}), 400

    @app.route("/api/features", methods=["GET"])
    @login_required
    def list_features():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            return jsonify({"error": "Azure DevOps not configured."}), 400
        epic_title = request.args.get("epic_title", "")
        if not epic_title:
            return jsonify({"error": "epic_title parameter required."}), 400
        try:
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
            features = get_features_from_epic(azdo, epic_title, s.azdo_area_path)
            return jsonify(features)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ── PBI Generation API ───────────────────────────────────────

    @app.route("/api/generate", methods=["POST"])
    @login_required
    def generate_pbi():
        s = current_user.settings
        openai_key = get_openai_key(s)
        if not openai_key:
            return jsonify({"error": "OpenAI API key not configured."}), 400

        data = request.get_json()
        user_request = data.get("request", "").strip()
        epic_title = data.get("epic_title", "")
        parent_feature_id = data.get("parent_feature_id")

        if not user_request:
            return jsonify({"error": "Request description is required."}), 400

        try:
            oai = OpenAI(api_key=openai_key)
            pat = get_azdo_pat(s)

            # Build features context
            features_context = ""
            selected_feature_id = "null"
            available_features = []

            if parent_feature_id:
                features_context = f"**PARENT FEATURE OVERRIDE**: Use feature ID {parent_feature_id}."
                selected_feature_id = str(parent_feature_id)
            elif epic_title and pat:
                azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)
                available_features = get_features_from_epic(azdo, epic_title, s.azdo_area_path)
                if available_features:
                    features_context = "**AVAILABLE PARENT FEATURES**:\n"
                    for f in available_features:
                        desc_preview = (f.get("description", "")[:100] + "...") if len(f.get("description", "")) > 100 else f.get("description", "No description")
                        features_context += f"- ID {f['id']}: {f['title']}\n  {desc_preview}\n\n"
                    selected_feature_id = "ID_FROM_LIST_OR_null"

            prompt_template = s.pbi_prompt or DEFAULT_PROMPT
            prompt = prompt_template.format(
                user_request=user_request,
                features_context=features_context,
                selected_feature_id=selected_feature_id,
            )

            response = oai.chat.completions.create(
                model=s.openai_model or "gpt-5",
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.choices[0].message.content.strip()
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
                    return jsonify({"error": f"Missing field: {field}"}), 400

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

            return jsonify(pbi_data)

        except json.JSONDecodeError:
            return jsonify({"error": "Failed to parse AI response as JSON. Try again."}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/create", methods=["POST"])
    @login_required
    def create_pbi():
        s = current_user.settings
        pat = get_azdo_pat(s)
        if not s or not pat:
            return jsonify({"error": "Azure DevOps not configured."}), 400

        data = request.get_json()
        pbi_data = data.get("pbi_data")
        next_sprint = data.get("next_sprint", True)
        backlog = data.get("backlog", False)
        update_existing = data.get("update_existing", False)

        if not pbi_data:
            return jsonify({"error": "PBI data is required."}), 400

        try:
            azdo = AzDoClient(s.azdo_org_url, s.azdo_project, pat)

            # Validate parent feature
            if pbi_data.get("parent_feature_id"):
                if not validate_parent_feature(azdo, pbi_data["parent_feature_id"]):
                    pbi_data["parent_feature_id"] = None

            # Resolve iteration
            iteration_path = None
            if not backlog:
                iteration_path = get_target_iteration_path(azdo, s.azdo_team, next_sprint)

            # Check for existing
            existing_id = find_existing_pbi_by_title(azdo, s.azdo_area_path, iteration_path, pbi_data["title"])

            if existing_id and update_existing:
                wi = update_pbi_in_azdo(azdo, existing_id, pbi_data)
                action = "updated"
            elif existing_id:
                return jsonify({
                    "error": f"PBI '{pbi_data['title']}' already exists (#{existing_id}). Enable 'Update existing' to overwrite.",
                    "existing_id": existing_id,
                }), 409
            else:
                wi = create_pbi_in_azdo(azdo, pbi_data, s.azdo_area_path, iteration_path)
                action = "created"

            pbi_id = wi.get("id")
            pbi_url = wi.get("_links", {}).get("html", {}).get("href")

            return jsonify({
                "status": "ok",
                "action": action,
                "id": pbi_id,
                "url": pbi_url,
                "iteration": iteration_path,
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
