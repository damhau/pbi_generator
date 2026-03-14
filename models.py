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
    openai_model = db.Column(db.String(64), default="gpt-5")

    # Azure DevOps
    azdo_org_url = db.Column(db.String(256), default="https://tfs.ext.icrc.org/ICRCCollection")
    azdo_project = db.Column(db.String(128), default="Hybrid Cloud Architecture")
    azdo_team = db.Column(db.String(128), default="Cloud Native")
    azdo_pat = db.Column(db.String(256), default="")
    azdo_area_path = db.Column(db.String(256), default=r"Hybrid Cloud Architecture\Cloud Native Delivery")

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


DEFAULT_PROMPT = """Can you create a scrum pbi for "{user_request}" to assign it to the right parent feature you can look at the list below.
Please use the format As a ..., I want to ... so that ...

{features_context}

Please return your response as a JSON object with the following structure:
{{
    "title": "title",
    "description": "description",
    "acceptance_criteria": [],
    "priority": "you estimation of the priority between 1 and 3 (must be an integer)",
    "effort": "you estimation of the effort in story points between 1 and 13 (must be an integer)",
    "tags": ["draft","additional tags if relevant but not more than 3"],
    "parent_feature_id": {selected_feature_id}
}}

Additional instructions for the acceptance criteria:
- do not do more than 5 acceptance criteria
- not need to add acceptance criteria like "Rollback plan validated" "review sign-off" and "Monitoring/alerting checks in place" and "Go/No-Go criteria"
- if it is an Openshift PBI always add "Gitops repository updated" in the acceptance criteria
- if it is an Openshift PBI always add "Deployed on IKSTEST and IKSPROD" in the acceptance criteria
- order the acceptance criteria in a logical order
"""
