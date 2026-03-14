"""Azure DevOps API client."""

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class AzDoClient:
    """Azure DevOps API client with authentication and common operations."""

    def __init__(self, org_url: str, project: str, pat: str, api_version: str = "7.0"):
        self.org_url = org_url.rstrip("/")
        self.project = project
        self.pat = pat
        self.api_version = api_version
        self._auth_header = self._create_auth_header()

    def _create_auth_header(self) -> dict:
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "User-Agent": "choco",
            "Accept": "application/json",
        }

    def request(self, method: str, url: str, headers: dict = None, **kwargs) -> Dict[str, Any]:
        h = self._auth_header.copy()
        if headers:
            h.update(headers)
        logger.debug("AzDO %s %s", method, url)
        resp = requests.request(method, url, headers=h, timeout=30, **kwargs)
        logger.debug("AzDO response: %s (%d bytes)", resp.status_code, len(resp.content))
        if resp.status_code >= 400:
            logger.info("AzDO error %s %s: HTTP %s - %s", method, url, resp.status_code, resp.text[:200])
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            logger.info("AzDO non-JSON response from %s %s: %s", method, url, resp.text[:200])
            raise RuntimeError(f"Failed to parse JSON response: {e}")

    def get_project_info(self) -> Dict[str, Any]:
        url = f"{self.org_url}/_apis/projects/{self.project}?api-version={self.api_version}"
        return self.request("GET", url)

    def query_work_items(self, wiql_query: str) -> Dict[str, Any]:
        url = f"{self.org_url}/{self.project}/_apis/wit/wiql?api-version={self.api_version}"
        return self.request("POST", url, headers={"Content-Type": "application/json"}, json={"query": wiql_query})

    def get_work_items(self, ids: List[int], fields: List[str] = None) -> Dict[str, Any]:
        ids_str = ",".join(map(str, ids))
        url = f"{self.org_url}/_apis/wit/workitems?ids={ids_str}&api-version={self.api_version}"
        if fields:
            url += f"&fields={','.join(fields)}"
        return self.request("GET", url)

    def get_work_item(self, work_item_id: int) -> Dict[str, Any]:
        url = f"{self.org_url}/_apis/wit/workitems/{work_item_id}?api-version={self.api_version}"
        return self.request("GET", url)

    def create_work_item(self, work_item_type: str, fields_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"{self.org_url}/{self.project}/_apis/wit/workitems/${work_item_type}?api-version={self.api_version}"
        return self.request("POST", url, headers={"Content-Type": "application/json-patch+json"}, json=fields_payload)

    def update_work_item(self, work_item_id: int, fields_payload: List[Dict[str, Any]]) -> Dict[str, Any]:
        url = f"{self.org_url}/_apis/wit/workitems/{work_item_id}?api-version={self.api_version}"
        return self.request("PATCH", url, headers={"Content-Type": "application/json-patch+json"}, json=fields_payload)

    def get_team_iterations(self, team: str, timeframe: str = "current") -> Dict[str, Any]:
        base = f"{self.org_url}/{self.project}/{team}/_apis/work/teamsettings/iterations"
        if timeframe:
            url = f"{base}?$timeframe={timeframe}&api-version={self.api_version}"
        else:
            url = f"{base}?api-version={self.api_version}"
        return self.request("GET", url)

    def query_wiql(self, wiql_query: str) -> Dict[str, Any]:
        return self.query_work_items(wiql_query)

    def get_current_iteration(self, team: str) -> Dict[str, Any]:
        return self.get_team_iterations(team, timeframe="current")


def _iso_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _escape_wiql_literal(s: str) -> str:
    return s.replace("'", "''")


def get_current_iteration_path(azdo: AzDoClient, team: str) -> str:
    data = azdo.get_current_iteration(team)
    vals = data.get("value", [])
    if vals:
        return vals[0]["path"]

    data = azdo.get_team_iterations(team, timeframe="")
    iters = data.get("value", [])
    if not iters:
        raise RuntimeError("No iterations defined for team.")

    now = datetime.now(timezone.utc)
    active = []
    for it in iters:
        attrs = it.get("attributes", {})
        sd, fd = attrs.get("startDate"), attrs.get("finishDate")
        if sd and fd:
            sdt, fdt = _iso_to_dt(sd), _iso_to_dt(fd)
            if sdt <= now <= fdt:
                active.append((sdt, it))
    if active:
        active.sort(key=lambda x: x[0], reverse=True)
        return active[0][1]["path"]

    future, past = [], []
    for it in iters:
        sd = it.get("attributes", {}).get("startDate")
        if sd:
            sdt = _iso_to_dt(sd)
            (future if sdt > now else past).append((sdt, it))
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]["path"]
    if past:
        past.sort(key=lambda x: x[0], reverse=True)
        return past[0][1]["path"]
    raise RuntimeError("No iterations found for team")


def get_next_iteration_path(azdo: AzDoClient, team: str) -> str:
    data = azdo.get_team_iterations(team, timeframe="")
    iters = data.get("value", [])
    if not iters:
        raise RuntimeError("No iterations defined for team.")

    now = datetime.now(timezone.utc)
    future = []
    for it in iters:
        sd = it.get("attributes", {}).get("startDate")
        if sd:
            sdt = _iso_to_dt(sd)
            if sdt > now:
                future.append((sdt, it))
    if not future:
        raise RuntimeError("No future iterations found")
    future.sort(key=lambda x: x[0])
    return future[0][1]["path"]


def get_target_iteration_path(azdo: AzDoClient, team: str, next_sprint: bool = False) -> str:
    if next_sprint:
        return get_next_iteration_path(azdo, team)
    return get_current_iteration_path(azdo, team)


def get_features_from_epic(azdo: AzDoClient, epic_title: str, area_path: str) -> List[Dict[str, Any]]:
    epic_title_esc = _escape_wiql_literal(epic_title)
    area_path_esc = _escape_wiql_literal(area_path)
    wiql = f"""
    SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType]
    FROM WorkItemLinks
    WHERE
        ([Source].[System.TeamProject] = @project
         AND [Source].[System.WorkItemType] = 'Epic'
         AND [Source].[System.Title] = '{epic_title_esc}')
        AND ([System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward')
        AND ([Target].[System.WorkItemType] = 'Feature')
        AND ([Target].[System.AreaPath] = '{area_path_esc}')
    MODE (MustContain)
    """
    data = azdo.query_wiql(wiql)
    relations = data.get("workItemRelations", [])
    if not relations:
        return []

    epic_ids = set()
    feature_ids = []
    for rel in relations:
        src = rel.get("source")
        tgt = rel.get("target")
        if src and src.get("id"):
            epic_ids.add(src["id"])
        if tgt and tgt.get("id") and tgt["id"] not in epic_ids:
            feature_ids.append(tgt["id"])
    feature_ids = list(set(feature_ids))
    if not feature_ids:
        return []

    features_resp = azdo.get_work_items(feature_ids)
    result = []
    for item in features_resp.get("value", []):
        fields = item.get("fields", {})
        if fields.get("System.WorkItemType") == "Feature" and fields.get("System.State") != "Removed":
            result.append({
                "id": item["id"],
                "title": fields.get("System.Title", ""),
                "description": fields.get("System.Description", ""),
            })
    return result


def get_epics(azdo: AzDoClient, area_path: str) -> List[Dict[str, Any]]:
    """Get all active epics in the given area path."""
    area_esc = _escape_wiql_literal(area_path)
    wiql = f"""
    SELECT [System.Id], [System.Title]
    FROM workitems
    WHERE
        [System.TeamProject] = @project
        AND [System.WorkItemType] = 'Epic'
        AND [System.State] <> 'Removed'
        AND [System.AreaPath] UNDER '{area_esc}'
    ORDER BY [System.Title] ASC
    """
    data = azdo.query_wiql(wiql)
    ids = [wi["id"] for wi in data.get("workItems", [])]
    if not ids:
        return []
    items_resp = azdo.get_work_items(ids)
    return [
        {"id": item["id"], "title": item["fields"].get("System.Title", "")}
        for item in items_resp.get("value", [])
    ]


def find_existing_pbi_by_title(azdo: AzDoClient, area_path: str, iteration_path: Optional[str], title: str) -> Optional[int]:
    title_esc = _escape_wiql_literal(title)
    area_esc = _escape_wiql_literal(area_path)
    iteration_clause = ""
    if iteration_path:
        it_esc = _escape_wiql_literal(iteration_path)
        iteration_clause = f"AND [System.IterationPath] = '{it_esc}'"
    wiql = f"""
    SELECT [System.Id]
    FROM workitems
    WHERE
        [System.TeamProject] = @project
        AND [System.WorkItemType] = 'Product Backlog Item'
        {iteration_clause}
        AND [System.Title] = '{title_esc}'
        AND [System.State] <> 'Removed'
        AND [System.AreaPath] UNDER '{area_esc}'
    ORDER BY [System.ChangedDate] DESC
    """
    data = azdo.query_wiql(wiql)
    ids = [wi["id"] for wi in data.get("workItems", [])]
    return ids[0] if ids else None


def validate_parent_feature(azdo: AzDoClient, feature_id: int, available_features: List[Dict[str, Any]] = None) -> bool:
    try:
        if available_features:
            if feature_id not in [f["id"] for f in available_features]:
                logger.debug("Feature %s not in available features list", feature_id)
                return False
        data = azdo.get_work_item(feature_id)
        fields = data.get("fields", {})
        if fields.get("System.WorkItemType") != "Feature":
            logger.debug("Work item %s is not a Feature (type=%s)", feature_id, fields.get("System.WorkItemType"))
            return False
        if fields.get("System.State") == "Removed":
            logger.debug("Feature %s is in Removed state", feature_id)
            return False
        return True
    except Exception as e:
        logger.info("Failed to validate parent feature %s: %s", feature_id, e)
        return False


def create_pbi_in_azdo(azdo: AzDoClient, pbi_data: Dict[str, Any], area_path: str, iteration_path: Optional[str]) -> Dict[str, Any]:
    title = pbi_data["title"]
    existing_id = find_existing_pbi_by_title(azdo, area_path, iteration_path, title)
    if existing_id:
        return azdo.get_work_item(existing_id)

    desc = f"<div>{pbi_data['description']}</div>"
    ac_html = "<ul>\n" + "".join(f"<li>{c}</li>\n" for c in pbi_data["acceptance_criteria"]) + "</ul>"
    tags_str = ";".join(pbi_data.get("tags", []))

    payload = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.Description", "value": desc},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": ac_html},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": pbi_data["priority"]},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Scheduling.Effort", "value": pbi_data["effort"]},
        {"op": "add", "path": "/fields/System.AreaPath", "value": area_path},
    ]
    if iteration_path:
        payload.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
    if tags_str:
        payload.append({"op": "add", "path": "/fields/System.Tags", "value": tags_str})

    created = azdo.create_work_item("Product Backlog Item", payload)
    pbi_id = created.get("id")

    parent_id = pbi_data.get("parent_feature_id")
    if parent_id and pbi_id:
        try:
            _create_parent_child_link(azdo, parent_id, pbi_id)
        except Exception as e:
            logger.warning("Failed to link parent feature: %s", e)

    return created


def update_pbi_in_azdo(azdo: AzDoClient, work_item_id: int, pbi_data: Dict[str, Any]) -> Dict[str, Any]:
    desc = f"<div>{pbi_data['description']}</div>"
    ac_html = "<ul>\n" + "".join(f"<li>{c}</li>\n" for c in pbi_data["acceptance_criteria"]) + "</ul>"
    tags_str = ";".join(pbi_data.get("tags", []))

    patch = [
        {"op": "replace", "path": "/fields/System.Description", "value": desc},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.AcceptanceCriteria", "value": ac_html},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": pbi_data["priority"]},
        {"op": "replace", "path": "/fields/Microsoft.VSTS.Scheduling.Effort", "value": pbi_data["effort"]},
        {"op": "add", "path": "/fields/System.History", "value": "Updated by PBI Generator."},
    ]
    if tags_str:
        patch.append({"op": "replace", "path": "/fields/System.Tags", "value": tags_str})

    updated = azdo.update_work_item(work_item_id, patch)

    parent_id = pbi_data.get("parent_feature_id")
    if parent_id:
        existing_rels = updated.get("relations", [])
        already_linked = any(
            r.get("rel") == "System.LinkTypes.Hierarchy-Reverse" and str(parent_id) in r.get("url", "")
            for r in existing_rels
        )
        if not already_linked:
            try:
                _create_parent_child_link(azdo, parent_id, work_item_id)
            except Exception as e:
                logger.warning("Failed to update parent link: %s", e)

    return updated


def _create_parent_child_link(azdo: AzDoClient, parent_id: int, child_id: int):
    payload = [{
        "op": "add",
        "path": "/relations/-",
        "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{azdo.org_url}/_apis/wit/workItems/{parent_id}",
            "attributes": {"comment": "Auto-linked by PBI Generator"},
        },
    }]
    return azdo.update_work_item(child_id, payload)
