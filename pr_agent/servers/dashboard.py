"""Centralized dashboard (issue #4) — minimal foundation.

Serves an HTML page, mounted on the existing GitHub App FastAPI server, that lists
every repository where the GitHub App is installed together with its open pull
requests. Agent processing status and refresh controls are intentionally left for a
follow-up step (see issue #4), but the data model below leaves room for them.
"""

import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates
from github import Github, GithubIntegration

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

router = APIRouter()

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


def _build_app_integration() -> GithubIntegration:
    """Build a JWT-authenticated GithubIntegration from the App credentials.

    Requires deployment_type == 'app' and github.app_id / github.private_key to be set.
    """
    deployment_type = get_settings().get("GITHUB.DEPLOYMENT_TYPE", "user")
    if deployment_type != "app":
        raise ValueError(
            "The dashboard requires GitHub App deployment (GITHUB.DEPLOYMENT_TYPE='app')."
        )
    try:
        app_id = get_settings().github.app_id
        private_key = get_settings().github.private_key
    except AttributeError as e:
        raise ValueError(
            "GitHub app ID and private key are required to render the dashboard."
        ) from e
    base_url = get_settings().get("GITHUB.BASE_URL", "https://api.github.com")
    return GithubIntegration(integration_id=app_id, private_key=private_key, base_url=base_url)


def _relative_time(dt_str: str) -> str:
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        if seconds < 86400 * 30:
            return f"{seconds // 86400}d ago"
        return f"{seconds // (86400 * 30)}mo ago"
    except Exception:
        return dt_str[:10] if len(dt_str) >= 10 else dt_str


def _get_closed_pr_count(requester, full_name: str) -> int:
    try:
        headers, data = requester.requestJsonAndCheck(
            "GET", f"/repos/{full_name}/pulls?state=closed&per_page=1"
        )
        link = headers.get("Link", "") or headers.get("link", "")
        m = re.search(r"page=(\d+)>; rel=\"last\"", link)
        if m:
            return int(m.group(1))
        return 1 if data else 0
    except Exception:
        return 0


def _list_installation_repos(client: Github) -> list:
    """List repositories accessible to an installation token (GET /installation/repositories)."""
    requester = client._Github__requester
    repos = []
    page = 1
    while True:
        _, data = requester.requestJsonAndCheck(
            "GET", f"/installation/repositories?per_page=100&page={page}"
        )
        batch = data.get("repositories", [])
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def collect_dashboard_data() -> list:
    """Collect installed repos and their open PRs across all App installations.

    Returns a list of dicts shaped as::

        {"repo_full_name": str, "repo_url": str, "pulls": [
            {"number": int, "title": str, "author": str,
             "created_at": str, "html_url": str}, ...]}

    Failures on a single installation or repo are logged and skipped so a single bad
    repo never breaks the whole page.
    """
    integration = _build_app_integration()
    base_url = get_settings().get("GITHUB.BASE_URL", "https://api.github.com")
    result = []

    try:
        installations = list(integration.get_installations())
    except Exception as e:
        get_logger().error(f"Dashboard: failed to list App installations: {e}")
        return result

    for installation in installations:
        try:
            token = integration.get_access_token(installation.id).token
        except Exception as e:
            get_logger().error(
                f"Dashboard: failed to get token for installation {getattr(installation, 'id', '?')}: {e}"
            )
            continue

        client = Github(login_or_token=token, base_url=base_url)
        try:
            repos = _list_installation_repos(client)
        except Exception as e:
            get_logger().error(
                f"Dashboard: failed to list repos for installation {installation.id}: {e}"
            )
            continue

        requester = client._Github__requester
        for repo in repos:
            full_name = repo.get("full_name")
            entry = {
                "repo_full_name": full_name,
                "repo_url": repo.get("html_url") or "",
                "description": repo.get("description") or "",
                "stars": repo.get("stargazers_count") or 0,
                "forks": repo.get("forks_count") or 0,
                "open_issues_count": repo.get("open_issues_count") or 0,
                "closed_prs": _get_closed_pr_count(requester, full_name),
                "updated_at": _relative_time(repo.get("updated_at", "")),
                "pulls": [],
            }
            try:
                repo_obj = client.get_repo(full_name)
                # Use repo_obj for reliable stats (installation API may omit fields)
                entry["stars"] = repo_obj.stargazers_count or 0
                entry["forks"] = repo_obj.forks_count or 0
                entry["open_issues_count"] = repo_obj.open_issues_count or 0
                entry["repo_url"] = repo_obj.html_url or entry["repo_url"]
                entry["description"] = repo_obj.description or ""
                if repo_obj.updated_at:
                    entry["updated_at"] = _relative_time(repo_obj.updated_at.isoformat())
                for pr in repo_obj.get_pulls(state="open"):
                    entry["pulls"].append(
                        {
                            "number": pr.number,
                            "title": pr.title,
                            "author": pr.user.login if pr.user else "",
                            "created_at": pr.created_at.isoformat() if pr.created_at else "",
                            "html_url": pr.html_url,
                            "status": "pending",
                        }
                    )
            except Exception as e:
                get_logger().error(f"Dashboard: failed to list PRs for {full_name}: {e}")
            result.append(entry)

    return result


def _check_access(request: Request) -> None:
    """Optional token guard. Open access when DASHBOARD.ACCESS_TOKEN is unset/empty."""
    expected = get_settings().get("DASHBOARD.ACCESS_TOKEN", "")
    if not expected:
        return
    provided = request.query_params.get("token") or request.headers.get("X-Dashboard-Token")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/dashboard")
async def dashboard(request: Request):
    _check_access(request)
    try:
        repos = collect_dashboard_data()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "repos": repos}
    )
