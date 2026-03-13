#!/usr/bin/env python3
"""
Scrum PBI Generator and Azure DevOps Integration Script

This script uses OpenAI's ChatGPT API to generate Scrum Product Backlog Items (PBIs)
and automatically creates them in Azure DevOps with proper sprint allocation.
"""

import os
import sys
import base64
import json
import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
import requests
from openai import OpenAI

import logging

# Create a named logger instance
logger = logging.getLogger("myapp")
logger.setLevel(logging.INFO)   # or INFO, WARNING, ERROR

# Optional: configure handler (console in this case)
handler = logging.StreamHandler()
# formatter = logging.Formatter("[%(levelname)s] %(message)s")
# handler.setFormatter(formatter)
logger.addHandler(handler)


# ---------- CONFIG ----------
# Azure DevOps Configuration
AZDO_ORG_URL = "https://tfs.ext.icrc.org/ICRCCollection"  # no trailing slash
AZDO_PROJECT = "Hybrid%20Cloud%20Architecture"
AZDO_TEAM = "Cloud Native"
AZDO_PAT = os.getenv("AZDO_PAT")
AREA_PATH = r"Hybrid Cloud Architecture\Cloud Native Delivery"  # Use raw/backslashes
API_VERSION = "7.0"

# OpenAI Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Initialize OpenAI client (lazy — checked at runtime in main() and app.py)
client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# Global Azure DevOps client (initialized in main)
azdo_client = None


class AzDoClient:
    """Azure DevOps API client with authentication and common operations."""

    def __init__(self, org_url: str, project: str, pat: str, api_version: str = "7.0"):
        self.org_url = org_url.rstrip('/')  # Remove trailing slash
        self.project = project
        self.pat = pat
        self.api_version = api_version
        self._auth_header = self._create_auth_header()

    def _create_auth_header(self) -> dict:
        """Create authentication header for Azure DevOps API calls."""
        token = base64.b64encode(f":{self.pat}".encode("utf-8")).decode("utf-8")
        return {
            "Authorization": f"Basic {token}",
            "User-Agent": "choco",
            "Accept": "application/json"
        }

    def request(self, method: str, url: str, headers: dict = None, **kwargs) -> Dict[str, Any]:
        """Make authenticated request to Azure DevOps API."""
        h = self._auth_header.copy()
        if headers:
            h.update(headers)

        resp = requests.request(method, url, headers=h, timeout=30, **kwargs)
        logger.debug(f"{method} {url} -> {resp.status_code}")

        # Check for HTTP errors
        if resp.status_code >= 400:
            logger.info(f"❌ HTTP Error {resp.status_code}: {method} {url}")
            logger.info(f"Response headers: {dict(resp.headers)}")
            logger.info(f"Response content: {resp.text[:500]}...")
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

        # Try to parse JSON response
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            logger.info(f"❌ JSON Parse Error: {e}")
            logger.info(f"Response status: {resp.status_code}")
            logger.info(f"Response headers: {dict(resp.headers)}")
            logger.info(f"Response content: {resp.text[:1000]}...")
            raise RuntimeError(f"Failed to parse JSON response: {e}")

    def get_project_info(self) -> Dict[str, Any]:
        """Get project information."""
        url = f"{self.org_url}/_apis/projects/{self.project}?api-version={self.api_version}"
        return self.request("GET", url)

    def query_work_items(self, wiql_query: str) -> Dict[str, Any]:
        """Execute a WIQL query."""
        url = f"{self.org_url}/{self.project}/_apis/wit/wiql?api-version={self.api_version}"
        payload = {"query": wiql_query}
        headers = {"Content-Type": "application/json"}
        return self.request("POST", url, headers=headers, json=payload)

    def get_work_items(self, ids: List[int], fields: List[str] = None) -> Dict[str, Any]:
        """Get work items by IDs."""
        ids_str = ",".join(map(str, ids))
        url = f"{self.org_url}/_apis/wit/workitems?ids={ids_str}&api-version={self.api_version}"
        if fields:
            fields_str = ",".join(fields)
            url += f"&fields={fields_str}"
        return self.request("GET", url)

    def get_work_item(self, work_item_id: int) -> Dict[str, Any]:
        """Get a single work item by ID."""
        url = f"{self.org_url}/_apis/wit/workitems/{work_item_id}?api-version={self.api_version}"
        return self.request("GET", url)

    def create_work_item(self, work_item_type: str, fields_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create a new work item."""
        url = f"{self.org_url}/{self.project}/_apis/wit/workitems/${work_item_type}?api-version={self.api_version}"
        headers = {"Content-Type": "application/json-patch+json"}
        return self.request("POST", url, headers=headers, json=fields_payload)

    def update_work_item(self, work_item_id: int, fields_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Update an existing work item."""
        url = f"{self.org_url}/_apis/wit/workitems/{work_item_id}?api-version={self.api_version}"
        headers = {"Content-Type": "application/json-patch+json"}
        return self.request("PATCH", url, headers=headers, json=fields_payload)

    def get_team_iterations(self, team: str, timeframe: str = "current") -> Dict[str, Any]:
        """Get team iterations."""
        if timeframe:
            url = f"{self.org_url}/{self.project}/{team}/_apis/work/teamsettings/iterations?$timeframe={timeframe}&api-version={self.api_version}"
        else:
            # Get all iterations when timeframe is empty
            url = f"{self.org_url}/{self.project}/{team}/_apis/work/teamsettings/iterations?api-version={self.api_version}"
        return self.request("GET", url)

    def query_wiql(self, wiql_query: str) -> Dict[str, Any]:
        """Execute a WIQL query (alias for query_work_items)."""
        return self.query_work_items(wiql_query)

    def get_current_iteration(self, team: str) -> Dict[str, Any]:
        """Get current iteration for a team."""
        return self.get_team_iterations(team, timeframe="current")


# Global Azure DevOps client (initialized in main)



# PBI Generation Prompt Template
PBI_GENERATION_PROMPT = """
Can you create a scrum pbi for "{user_request}" to assign it to the right parent feature you can look at the list below.
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
# ---------------------------


def generate_pbi_with_chatgpt(user_request: str, available_features: List[Dict[str, Any]] = None, parent_feature_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Generate a Scrum PBI using ChatGPT API.

    Args:
        user_request: The user's request for the PBI
        available_features: List of available features from the epic for parent selection
        parent_feature_id: Optional manual override for parent feature

    Returns:
        Dictionary containing PBI data (title, description, acceptance_criteria, etc.)

    Raises:
        Exception: If API call fails or response is invalid
    """
    try:
        # Prepare features context for the prompt
        features_context = ""
        feature_selection_instruction = "Set to null if no appropriate parent feature exists"
        selected_feature_id = "null"

        if parent_feature_id:
            # Manual override provided
            features_context = f"**PARENT FEATURE OVERRIDE**: Use feature ID {parent_feature_id} as the parent."
            feature_selection_instruction = f"Use the provided parent feature ID: {parent_feature_id}"
            selected_feature_id = str(parent_feature_id)
        elif available_features:
            # AI should select from available features
            features_context = "**AVAILABLE PARENT FEATURES** (from Technical Foundation epic):\n"
            for feature in available_features:
                desc_preview = (feature.get('description', '')[:100] + '...'
                              if len(feature.get('description', '')) > 100
                              else feature.get('description', 'No description'))
                features_context += f"- ID {feature['id']}: {feature['title']}\n  {desc_preview}\n\n"

            feature_selection_instruction = ("Analyze the PBI request and select the most appropriate parent feature ID from the list above. "
                                           "Choose based on technical alignment, domain similarity, and logical grouping. "
                                           "If none are appropriate, set to null.")
            selected_feature_id = "ID_FROM_LIST_OR_null"

        prompt = PBI_GENERATION_PROMPT.format(
            user_request=user_request,
            features_context=features_context,
            feature_selection_instruction=feature_selection_instruction,
            selected_feature_id=selected_feature_id
        )

        response = client.chat.completions.create(
            model="gpt-5",
            messages=[
                #{"role": "system", "content": "You are a Senior Cloud Engineer expert in Openshift, Azure and Terraform and you are also able to create well-structured Product Backlog Items and selecting appropriate parent features."},
                {"role": "user", "content": prompt}
            ]
        )

        content = response.choices[0].message.content.strip()
        logger.info(f"ChatGPT response:\n{content}\n")

        # Try to extract JSON from the response
        try:
            # Remove any markdown code blocks if present
            if content.startswith("```json"):
                content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            pbi_data = json.loads(content)
        except json.JSONDecodeError as e:
            raise Exception(f"Failed to parse ChatGPT response as JSON: {e}\nResponse: {content}")

        # Validate required fields
        required_fields = ["title", "description", "acceptance_criteria", "priority", "effort"]
        missing_fields = [field for field in required_fields if field not in pbi_data]
        if missing_fields:
            raise Exception(f"Missing required fields in ChatGPT response: {missing_fields}")

        # Validate data types and ranges
        if not isinstance(pbi_data["title"], str) or len(pbi_data["title"]) > 255:
            raise Exception("Title must be a string with max 255 characters")

        if not isinstance(pbi_data["description"], str):
            raise Exception("Description must be a string")

        if not isinstance(pbi_data["acceptance_criteria"], list):
            raise Exception("Acceptance criteria must be a list")

        if not isinstance(pbi_data["priority"], int) or pbi_data["priority"] not in [1, 2, 3, 4]:
            raise Exception("Priority must be an integer between 1-4")

        if not isinstance(pbi_data["effort"], (int, float)) or pbi_data["effort"] < 1 or pbi_data["effort"] > 13:
            raise Exception("Effort must be a integer between 1-13")

        # Set default tags if not provided
        if "tags" not in pbi_data:
            pbi_data["tags"] = []

        # Handle parent feature ID
        parent_id = pbi_data.get("parent_feature_id")
        if parent_id and parent_id != "null":
            try:
                pbi_data["parent_feature_id"] = int(parent_id)
            except (ValueError, TypeError):
                logger.info(f"⚠️  Invalid parent feature ID '{parent_id}', setting to None")
                pbi_data["parent_feature_id"] = None
        else:
            pbi_data["parent_feature_id"] = None

        return pbi_data

    except Exception as e:
        raise Exception(f"Failed to generate PBI with ChatGPT: {e}")


def get_default_team_name() -> str:
    """Get the default team name for a project."""
    # Try to get project's default team
    data = azdo_client.get_project_info()
    default_team = data.get("defaultTeam") or {}
    name = default_team.get("name")
    if name:
        return name

    # Fallback: list teams and pick the first
    teams_data = azdo_client.request("GET", f"{azdo_client.org_url}/_apis/projects/{azdo_client.project}/teams?api-version={azdo_client.api_version}")
    teams = teams_data.get("value", [])
    if not teams:
        raise RuntimeError("No teams found in project; cannot resolve team context.")
    return teams[0]["name"]


def _iso_to_dt(s: str) -> datetime:
    """Convert ADO ISO timestamp to datetime object."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def get_current_iteration_path(team: Optional[str] = None) -> str:
    """Get the current iteration path for a team."""
    if not team:
        team = get_default_team_name()

    # 1) Try explicit "current" timeframe
    data = azdo_client.get_current_iteration(team)
    vals = data.get("value", [])
    if vals:
        return vals[0]["path"]

    # 2) If nothing is "current", fetch all iterations and pick by date window
    data = azdo_client.get_team_iterations(team)
    iters = data.get("value", [])
    if not iters:
        raise RuntimeError("No iterations defined for team; cannot compute current sprint.")

    now = datetime.now(timezone.utc)
    # Prefer iteration where now is between start & finish
    active = []
    for it in iters:
        attrs = it.get("attributes", {})
        sd, fd = attrs.get("startDate"), attrs.get("finishDate")
        if sd and fd:
            sdt = _iso_to_dt(sd)
            fdt = _iso_to_dt(fd)
            if sdt <= now <= fdt:
                active.append((sdt, it))
    if active:
        # If multiple, pick the one with latest start
        active.sort(key=lambda x: x[0], reverse=True)
        return active[0][1]["path"]

    # 3) Otherwise pick the nearest future; else latest past
    future = []
    past = []
    for it in iters:
        attrs = it.get("attributes", {})
        sd = attrs.get("startDate")
        if sd:
            sdt = _iso_to_dt(sd)
            if sdt > now:
                future.append((sdt, it))
            else:
                past.append((sdt, it))
    if future:
        future.sort(key=lambda x: x[0])  # soonest upcoming
        return future[0][1]["path"]
    if past:
        past.sort(key=lambda x: x[0], reverse=True)  # most recent past
        return past[0][1]["path"]

    raise RuntimeError("No iterations found for team")


def get_next_iteration_path(team: Optional[str] = None) -> str:
    """Get the next iteration path for a team."""
    if not team:
        team = get_default_team_name()

    # Get all iterations, not just current
    data = azdo_client.get_team_iterations(team, timeframe="")
    iters = data.get("value", [])
    if not iters:
        raise RuntimeError("No iterations defined for team; cannot compute next sprint.")

    now = datetime.now(timezone.utc)
    future = []
    for it in iters:
        attrs = it.get("attributes", {})
        sd = attrs.get("startDate")
        if sd:
            sdt = _iso_to_dt(sd)
            if sdt > now:
                future.append((sdt, it))

    if not future:
        raise RuntimeError("No future iterations found")

    # Sort by start date and return the earliest future iteration
    future.sort(key=lambda x: x[0])
    return future[0][1]["path"]


def get_target_iteration_path(team: Optional[str] = None, next_sprint: bool = False) -> str:
    """Get the target iteration path based on whether next sprint is requested."""
    if next_sprint:
        return get_next_iteration_path(team)
    else:
        return get_current_iteration_path(team)


def get_features_from_epic(epic_title: str = "Technical Foundation") -> List[Dict[str, Any]]:
    """Get all features under a specific epic using WorkItemLinks query."""
    epic_title_esc = _escape_wiql_literal(epic_title)
    hierarchy_wiql = f"""
    SELECT
        [System.Id],
        [System.Title],
        [System.State],
        [System.WorkItemType],
        [System.AreaPath],
        [System.IterationPath]
    FROM WorkItemLinks
    WHERE
        (
            [Source].[System.TeamProject] = @project
            AND [Source].[System.WorkItemType] = 'Epic'
            AND [Source].[System.Title] = '{epic_title_esc}'
        )
        AND
        (
            [System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward'

        )
        AND
        (
            [Target].[System.WorkItemType] = 'Feature'
        )
        AND (
        [Target].[System.AreaPath] = 'Hybrid Cloud Architecture\\Cloud Native Delivery'
        )
    MODE (MustContain)
    """
    logger.info(f"🔍 Finding features under epic '{epic_title}' using WorkItemLinks query...")
    hierarchy_data = azdo_client.query_wiql(hierarchy_wiql)
    relations = hierarchy_data.get("workItemRelations", [])
    logger.info(f"🔍 Found {len(relations)} hierarchy relations")
    if not relations:
        logger.info(f"⚠️  No features found under epic '{epic_title}'")
        return []
    feature_ids = []
    epic_ids = set()
    for relation in relations:
        source = relation.get("source")
        target = relation.get("target")
        if source and source.get("id"):
            epic_ids.add(source["id"])
        if target and target.get("id"):
            target_id = target["id"]
            if target_id not in epic_ids:
                feature_ids.append(target_id)
    feature_ids = list(set(feature_ids))
    logger.info(f"🔍 Getting detailed information for {len(feature_ids)} unique features...")
    if not feature_ids:
        logger.info(f"⚠️  No feature IDs extracted from relations")
        return []
    features_response = azdo_client.get_work_items(feature_ids)
    features_data = features_response.get("value", [])
    epic_features = []
    for item in features_data:
        fields = item.get("fields", {})
        work_item_type = fields.get("System.WorkItemType", "")
        state = fields.get("System.State", "")
        area_path = fields.get("System.AreaPath", "")
        if (work_item_type == "Feature" and state != "Removed"):
            epic_features.append({
                "id": item.get("id"),
                "title": fields.get("System.Title", ""),
                "description": fields.get("System.Description", "")
            })
    logger.info(f"✅ Found {len(epic_features)} active features under epic '{epic_title}'")
    return epic_features


def _escape_wiql_literal(s: str) -> str:
    """Escape single quotes for WIQL string literals."""
    return s.replace("'", "''")


def find_existing_pbi_by_title(iteration_path: Optional[str], title: str) -> Optional[int]:
    """Find existing PBI by title. If iteration_path is provided, limit search to that sprint; otherwise search in Cloud Native area regardless of sprint."""
    title_esc = _escape_wiql_literal(title)
    if iteration_path:
        it_esc = _escape_wiql_literal(iteration_path)
        wiql = f"""
        SELECT [System.Id], [System.ChangedDate]
        FROM workitems
        WHERE
            [System.TeamProject] = @project
            AND [System.WorkItemType] = 'Product Backlog Item'
            AND [System.IterationPath] = '{it_esc}'
            AND [System.Title] = '{title_esc}'
            AND [System.State] <> 'Removed'
            AND [System.AreaPath] UNDER 'Hybrid Cloud Architecture\\Cloud Native Delivery'
        ORDER BY [System.ChangedDate] DESC
        """
    else:
        wiql = f"""
        SELECT [System.Id], [System.ChangedDate]
        FROM workitems
        WHERE
            [System.TeamProject] = @project
            AND [System.WorkItemType] = 'Product Backlog Item'
            AND [System.Title] = '{title_esc}'
            AND [System.State] <> 'Removed'
            AND [System.AreaPath] UNDER 'Hybrid Cloud Architecture\\Cloud Native Delivery'
        ORDER BY [System.ChangedDate] DESC
        """
    data = azdo_client.query_wiql(wiql)
    ids = [wi["id"] for wi in data.get("workItems", [])]
    return ids[0] if ids else None


def create_pbi_in_azdo(
    pbi_data: Dict[str, Any], area_path: str, iteration_path: Optional[str]
) -> Dict[str, Any]:
    """Create a new PBI in Azure DevOps."""
    title = pbi_data["title"]
    description = pbi_data["description"]
    acceptance_criteria = pbi_data["acceptance_criteria"]
    priority = pbi_data["priority"]
    effort = pbi_data["effort"]
    tags = pbi_data.get("tags", [])
    parent_feature_id = pbi_data.get("parent_feature_id")

    # Check if PBI already exists
    existing_id = find_existing_pbi_by_title(iteration_path, title)
    if existing_id:
        logger.info(f"⚠️  Found existing PBI titled '{title}' in '{iteration_path}': #{existing_id}")
        logger.info("Skipping creation to avoid duplicates. Use update functionality if needed.")
        # Return existing PBI info
        return azdo_client.get_work_item(existing_id)

    # Format description (without acceptance criteria)
    formatted_description = f"<div>{description}</div>"

    # Format acceptance criteria as HTML list
    formatted_acceptance_criteria = "<ul>\n"
    for criteria in acceptance_criteria:
        formatted_acceptance_criteria += f"<li>{criteria}</li>\n"
    formatted_acceptance_criteria += "</ul>"

    # Format tags as semicolon-separated string
    tags_string = ";".join(tags) if tags else ""

    logger.info(f"Creating new PBI titled '{title}' in '{iteration_path}'...")

    payload = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.Description", "value": formatted_description},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": formatted_acceptance_criteria},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.Effort", "value": effort},
        {"op": "add", "path": "/fields/System.AreaPath", "value": area_path},
    ]
    # Only assign iteration when provided (non-backlog mode)
    if iteration_path:
        payload.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})

    # Add tags if present
    if tags_string:
        payload.append({"op": "add", "path": "/fields/System.Tags", "value": tags_string})

    # Create the PBI
    created_pbi = azdo_client.create_work_item("Product Backlog Item", payload)
    pbi_id = created_pbi.get('id')

    # Create parent-child relationship if parent feature is specified
    if parent_feature_id and pbi_id:
        try:
            create_parent_child_relationship(parent_feature_id, pbi_id)
            logger.info(f"✅ Linked PBI #{pbi_id} to parent feature #{parent_feature_id}")
        except Exception as e:
            logger.info(f"⚠️  Warning: Failed to create parent-child relationship: {e}")
            # Don't fail the entire operation, just warn

    return created_pbi


def create_parent_child_relationship(parent_id: int, child_id: int):
    """Create a parent-child relationship between a feature and PBI."""

    # Add parent link using the hierarchy relationship
    relationship_payload = [{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{azdo_client.org_url}/_apis/wit/workItems/{parent_id}",
            "attributes": {
                "comment": "Auto-linked by PBI generator"
            }
        }
    }]

    # Use the azdo_client to patch the work item
    return azdo_client.update_work_item(child_id, relationship_payload)


def update_pbi_description(work_item_id: int, pbi_data: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing PBI's description and other fields."""
    description = pbi_data["description"]
    acceptance_criteria = pbi_data["acceptance_criteria"]
    priority = pbi_data["priority"]
    effort = pbi_data["effort"]
    tags = pbi_data.get("tags", [])
    parent_feature_id = pbi_data.get("parent_feature_id")

    # Format description (without acceptance criteria)
    formatted_description = f"<div>{description}</div>"

    # Format acceptance criteria as HTML list
    formatted_acceptance_criteria = "<ul>\n"
    for criteria in acceptance_criteria:
        formatted_acceptance_criteria += f"<li>{criteria}</li>\n"
    formatted_acceptance_criteria += "</ul>"

    # Format tags as semicolon-separated string
    tags_string = ";".join(tags) if tags else ""

    patch = [
        {"op": "replace", "path": "/fields/System.Description", "value": formatted_description},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": formatted_acceptance_criteria},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Scheduling.Effort", "value": effort},
        {"op": "add", "path": "/fields/System.History", "value": "Auto-updated from PBI generator script."}
    ]

    # Update tags if present
    if tags_string:
        patch.append({"op": "replace", "path": "/fields/System.Tags", "value": tags_string})

    updated_pbi = azdo_client.update_work_item(work_item_id, patch)

    # Update parent-child relationship if parent feature is specified
    if parent_feature_id:
        try:
            # First, check if relationship already exists
            existing_relations = updated_pbi.get('relations', [])
            parent_exists = any(
                rel.get('rel') == 'System.LinkTypes.Hierarchy-Reverse' and
                str(parent_feature_id) in rel.get('url', '')
                for rel in existing_relations
            )

            if not parent_exists:
                create_parent_child_relationship(parent_feature_id, work_item_id)
                logger.info(f"✅ Linked PBI #{work_item_id} to parent feature #{parent_feature_id}")
            else:
                logger.info(f"ℹ️  PBI #{work_item_id} already linked to feature #{parent_feature_id}")
        except Exception as e:
            logger.info(f"⚠️  Warning: Failed to update parent-child relationship: {e}")

    return updated_pbi


def validate_parent_feature(feature_id: int, available_features: List[Dict[str, Any]] = None) -> bool:
    """Validate that a parent feature ID exists and is accessible."""
    try:
        # Check if feature exists in available features list first
        if available_features:
            feature_ids = [f['id'] for f in available_features]
            if feature_id not in feature_ids:
                logger.info(f"⚠️  Feature #{feature_id} not found in available features from Technical Foundation epic")
                return False

        # Verify feature exists in Azure DevOps
        feature_data = azdo_client.get_work_item(feature_id)

        feature_type = feature_data.get('fields', {}).get('System.WorkItemType')
        if feature_type != 'Feature':
            logger.info(f"⚠️  Work item #{feature_id} is not a Feature (found: {feature_type})")
            return False

        feature_state = feature_data.get('fields', {}).get('System.State')
        if feature_state == 'Removed':
            logger.info(f"⚠️  Feature #{feature_id} is in 'Removed' state")
            return False

        return True

    except Exception as e:
        logger.info(f"⚠️  Failed to validate feature #{feature_id}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate and create Scrum PBIs using ChatGPT and Azure DevOps")
    parser.add_argument("request", nargs="?", help="The PBI request description")
    parser.add_argument("--ask", action="store_true", help="Prompt for request information interactively")
    parser.add_argument("--next-sprint", default=True, action="store_true", help="Assign to next sprint instead of current")
    parser.add_argument("--backlog", action="store_true", help="Create the PBI without assigning it to a sprint")
    parser.add_argument("--dry-run", action="store_true", help="Generate PBI but don't create in Azure DevOps")
    parser.add_argument("--update", action="store_true", help="Update existing PBI if found instead of skipping")
    parser.add_argument("--parent-feature", type=int, help="Override automatic feature selection with specific feature ID")
    parser.add_argument("--epic-title", default="Technical Foundation", help="Epic title to search for parent features (default: Technical Foundation)")

    args = parser.parse_args()

    # Handle request input
    if args.ask:
        # Interactive mode - prompt for request information
        logger.info("🤔 Interactive mode: Please provide the PBI request information")
        try:
            request = input("Enter the PBI request description: ").strip()
        except (KeyboardInterrupt, EOFError):
            logger.info("\n🛑 Operation cancelled by user")
            sys.exit(1)
    elif args.request:
        # Command line argument provided
        request = args.request
    else:
        # No request provided and not in interactive mode
        logger.info("❌ Error: PBI request is required. Use 'request' argument or --ask flag")
        parser.print_help()
        sys.exit(1)

    # Validate inputs
    if not request.strip():
        logger.info("❌ Error: PBI request cannot be empty")
        sys.exit(1)

    if not OPENAI_API_KEY:
        logger.info("❌ Error: OPENAI_API_KEY environment variable is required")
        sys.exit(1)

    global client
    if client is None:
        client = OpenAI(api_key=OPENAI_API_KEY)

    if not AZDO_PAT:
        logger.info("❌ Error: AZDO_PAT environment variable is required")
        sys.exit(1)

    logger.info(f"🚀 Generating PBI for request: {request}")
    if args.backlog:
        logger.info("📂 Target: Backlog (no sprint)")
    elif args.next_sprint:
        logger.info("📅 Target: Next sprint")
    else:
        logger.info("📅 Target: Current sprint")

    if args.parent_feature:
        logger.info(f"🎯 Parent feature override: #{args.parent_feature}")
    else:
        logger.info(f"🤖 Auto-selecting parent feature from epic: {args.epic_title}")

    try:
        # Validate Azure DevOps connectivity
        logger.info("\n🔗 Connecting to Azure DevOps...")

        # Initialize the Azure DevOps client
        global azdo_client
        azdo_client = AzDoClient(AZDO_ORG_URL, AZDO_PROJECT, AZDO_PAT)

        # Test connectivity by getting project info
        try:
            azdo_client.get_project_info()
            logger.info("✅ Connected to Azure DevOps")
        except Exception as e:
            raise Exception(f"Failed to connect to Azure DevOps: {e}")

        # Get available features for AI selection (unless manual override)
        available_features = None
        if not args.parent_feature:
            logger.info(f"\n📋 Fetching features from epic '{args.epic_title}'...")
            available_features = get_features_from_epic(args.epic_title)
            if not available_features:
                logger.info(f"⚠️  No features found in epic '{args.epic_title}'. PBI will be created without parent feature.")
        else:
            # Validate manual parent feature override
            logger.info(f"\n🔍 Validating parent feature #{args.parent_feature}...")
            if not validate_parent_feature(args.parent_feature):
                logger.info(f"❌ Invalid parent feature #{args.parent_feature}. Exiting.")
                sys.exit(1)
            logger.info(f"✅ Parent feature #{args.parent_feature} validated")

        # Generate PBI using ChatGPT
        logger.info("\n🤖 Generating PBI with ChatGPT...")
        pbi_data = generate_pbi_with_chatgpt(request, available_features, args.parent_feature)
        logger.info(f"✅ Generated PBI: {pbi_data['title']}")
        logger.info(f"📊 Priority: {pbi_data['priority']}, Effort: {pbi_data['effort']} points")

        # Validate AI-selected parent feature if present
        if pbi_data.get('parent_feature_id') and not args.parent_feature:
            logger.info(f"\n🔍 Validating AI-selected parent feature #{pbi_data['parent_feature_id']}...")
            if not validate_parent_feature(pbi_data['parent_feature_id'], available_features):
                logger.info(f"⚠️  AI-selected parent feature #{pbi_data['parent_feature_id']} is invalid, removing parent assignment")
                pbi_data['parent_feature_id'] = None
            else:
                logger.info(f"✅ AI-selected parent feature #{pbi_data['parent_feature_id']} validated")

        # Resolve and attach parent feature name (for dry-run visibility and logs)
        if pbi_data.get('parent_feature_id'):
            parent_feature_title = None
            # First try from the already fetched features list
            if available_features:
                for feature in available_features:
                    if feature['id'] == pbi_data['parent_feature_id']:
                        parent_feature_title = feature['title']
                        break
            # If not found (e.g., when using --parent-feature), fetch from ADO
            if parent_feature_title is None:
                try:
                    _wi_parent = azdo_client.get_work_item(pbi_data['parent_feature_id'])
                    parent_feature_title = _wi_parent.get('fields', {}).get('System.Title')
                except Exception:
                    parent_feature_title = None
            # Add to output structure for dry-run
            pbi_data['parent_feature_name'] = parent_feature_title
            logger.info(f"✅ Selected parent feature: #{pbi_data['parent_feature_id']} {parent_feature_title or 'Unknown'}")
        else:
            pbi_data['parent_feature_name'] = None
            logger.info("👤 No parent feature selected")

        if args.dry_run:
            logger.info("\n🔍 Dry run mode - PBI data generated but not created in Azure DevOps:")
            logger.info(json.dumps(pbi_data, indent=2))
            return

        # Get target iteration unless backlog requested
        if args.backlog:
            iteration_path = None
            logger.info("\n📋 Backlog mode: no sprint will be assigned")
        else:
            logger.info(f"\n📋 Resolving {'next' if args.next_sprint else 'current'} sprint...")
            iteration_path = get_target_iteration_path(AZDO_TEAM, args.next_sprint)
            logger.info(f"✅ Target iteration: {iteration_path}")

        # Create or update PBI in Azure DevOps
        logger.info("\n🎯 Creating PBI in Azure DevOps...")

        # Check for existing PBI first
        existing_id = find_existing_pbi_by_title(iteration_path, pbi_data["title"])

        if existing_id and args.update:
            logger.info(f"📝 Updating existing PBI #{existing_id}...")
            wi = update_pbi_description(existing_id, pbi_data)
            action = "updated"
        elif existing_id:
            logger.info(f"⚠️  PBI with title '{pbi_data['title']}' already exists (#{existing_id})")
            logger.info("Use --update flag to update the existing PBI")
            return
        else:
            wi = create_pbi_in_azdo(pbi_data, AREA_PATH, iteration_path)
            action = "created"

        # Success output
        pbi_id = wi.get('id')
        pbi_url = wi.get('_links', {}).get('html', {}).get('href')

        logger.info(f"\n🎉 Successfully {action} PBI #{pbi_id}")
        logger.info(f"📖 Title: {pbi_data['title']}")
        logger.info(f"📊 Priority: {pbi_data['priority']}, Effort: {pbi_data['effort']} points")
        if iteration_path:
            logger.info(f"📅 Iteration: {iteration_path}")
        else:
            logger.info("📂 Iteration: Backlog (no sprint)")
        if pbi_data.get('parent_feature_id'):
            logger.info(f"👨‍👩‍👧‍👦 Parent feature: #{pbi_data['parent_feature_id']}")
        if pbi_url:
            logger.info(f"🔗 URL: {pbi_url}")

    except KeyboardInterrupt:
        logger.info("\n🛑 Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.info(f"\n❌ Error: {e}")
        if "401" in str(e) or "Unauthorized" in str(e):
            logger.info("💡 Tip: Check your AZDO_PAT environment variable")
        elif "403" in str(e) or "Forbidden" in str(e):
            logger.info("💡 Tip: Verify your Azure DevOps permissions")
        elif "openai" in str(e).lower():
            logger.info("💡 Tip: Check your OPENAI_API_KEY environment variable")
        sys.exit(1)


if __name__ == "__main__":
    main()
