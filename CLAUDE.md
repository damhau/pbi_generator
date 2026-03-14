# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PBI Generator — a Flask web app that uses OpenAI to generate Azure DevOps Product Backlog Items (PBIs) from natural language descriptions. Multi-user with per-user settings for OpenAI keys and AzDO credentials.

## Git and pipeline

Each time you have finished a feature you can if I want you to commit and push, if I answer yes you can check the progress of the github build pipeline and return the docker image tag in the format sha-<number>


## Commands

```bash
# Run locally (dev server with hot reload)
uv run python app.py

# Run with debug logging
LOG_LEVEL=DEBUG uv run python app.py

# Install dependencies
uv sync

# Production (how the Docker image runs it)
uv run gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 "app:create_app()"

# Build Docker image
docker build -t pbi_generator .

# Check CI build status
gh run list --limit 5
```

No test suite exists. No linter is configured.

## Architecture

**Backend** (`app.py`) — single Flask module using the factory pattern (`create_app()`). All routes are defined inside the factory. PBI generation is async: `POST /api/generate` starts a background thread and returns a `job_id` (HTTP 202), then `GET /api/generate/<job_id>` is polled for status. The in-memory job store (`_jobs` dict) auto-evicts after 10 minutes.

**Models** (`models.py`) — SQLAlchemy with SQLite. Two tables: `User` (auth via flask-login + bcrypt) and `UserSettings` (OpenAI key, AzDO credentials, custom prompt). Users can use system-provided keys or their own (`use_own_openai_key`, `use_own_azdo_pat` flags). The `DEFAULT_PROMPT` template lives here.

**AzDO Client** (`azdo_client.py`) — stateless REST client for Azure DevOps. Handles WIQL queries, work item CRUD, iteration resolution, and parent-child linking. All functions take an `AzDoClient` instance.

**Frontend** (`static/js/app.js`, `static/css/style.css`) — vanilla JS, no build step. The generate flow submits a job then polls every 2s, updating the button spinner with the real backend stage (`fetching_features` → `calling_ai` → `parsing_response`).

**Templates** (`templates/`) — Jinja2, extending `base.html`.

## Key patterns

- System vs personal keys: `get_openai_key()` and `get_azdo_pat()` in `app.py` resolve which credentials to use based on user settings and env vars (`SYSTEM_OPENAI_API_KEY`, `SYSTEM_AZDO_PAT`).
- Admin check: hardcoded `ADMIN_USERNAMES` set in `app.py`.
- DB migrations are done inline in `create_app()` using `ALTER TABLE` checks (no Alembic).

## Deployment

Deployed to OpenShift. Kubernetes manifests are in `deploy/` (Deployment, Service, Route, PVC). CI builds and pushes to Docker Hub (`damienh/pbi_generator`) on every push to `main` via `.github/workflows/docker-publish.yml`. Image tags: `latest`, `main`, and `sha-<short>`.

## Environment variables

- `SECRET_KEY` — Flask session secret
- `DATABASE_URL` — SQLAlchemy URI (default: `sqlite:///pbi_generator.db`)
- `SYSTEM_OPENAI_API_KEY` — shared OpenAI key for all users
- `SYSTEM_AZDO_PAT` — shared Azure DevOps PAT
- `LOG_LEVEL` — logging level (default: `INFO`)
