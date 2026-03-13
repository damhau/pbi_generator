"""Database models for multi-user PBI Generator."""

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
bcrypt = Bcrypt()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    settings = db.relationship("UserSettings", backref="user", uselist=False, cascade="all, delete-orphan")

    def set_password(self, password: str):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")

    def check_password(self, password: str) -> bool:
        return bcrypt.check_password_hash(self.password_hash, password)


class UserSettings(db.Model):
    __tablename__ = "user_settings"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)

    # OpenAI
    openai_api_key = db.Column(db.String(256), default="")
    openai_model = db.Column(db.String(64), default="gpt-4o")

    # Azure DevOps
    azdo_org_url = db.Column(db.String(256), default="")
    azdo_project = db.Column(db.String(128), default="")
    azdo_team = db.Column(db.String(128), default="")
    azdo_pat = db.Column(db.String(256), default="")
    azdo_area_path = db.Column(db.String(256), default="")

    # PBI generation prompt
    pbi_prompt = db.Column(db.Text, default="")

    def to_dict(self) -> dict:
        return {
            "openai_api_key": self.openai_api_key or "",
            "openai_model": self.openai_model or "gpt-4o",
            "azdo_org_url": self.azdo_org_url or "",
            "azdo_project": self.azdo_project or "",
            "azdo_team": self.azdo_team or "",
            "azdo_pat": self.azdo_pat or "",
            "azdo_area_path": self.azdo_area_path or "",
            "pbi_prompt": self.pbi_prompt or DEFAULT_PROMPT,
        }


DEFAULT_PROMPT = """Create a scrum PBI for "{user_request}" and assign it to the right parent feature from the list below.
Use the format: As a ..., I want to ... so that ...

{features_context}

Return a JSON object:
{{
    "title": "title",
    "description": "description",
    "acceptance_criteria": [],
    "priority": <1-3>,
    "effort": <1-13>,
    "tags": ["draft"],
    "parent_feature_id": {selected_feature_id}
}}

Rules for acceptance criteria:
- Maximum 5 items
- No generic criteria like "Rollback plan validated", "Review sign-off", "Monitoring/alerting checks"
- If OpenShift related, add "Gitops repository updated" and "Deployed on IKSTEST and IKSPROD"
- Order logically
"""
