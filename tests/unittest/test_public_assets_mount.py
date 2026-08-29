"""The /public mount must never make github_app unimportable.

github_action_runner imports this module only for handle_line_comments, and the
action image used to ship no public/ — StaticFiles raises from its constructor,
so a missing directory took down the whole action.
"""

import pytest
from fastapi import FastAPI

import pr_agent.servers.github_app as github_app


def _public_routes(app: FastAPI):
    return [r for r in app.routes if getattr(r, "path", None) == "/public"]


def test_mount_skips_a_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(github_app, "base_path", str(tmp_path / "nowhere" / "pkg"))
    app = FastAPI()

    github_app._mount_public_assets(app)  # must not raise

    assert _public_routes(app) == [], "nothing is mounted when the directory is absent"


def test_mount_serves_an_existing_directory(tmp_path, monkeypatch):
    (tmp_path / "public").mkdir()
    monkeypatch.setattr(github_app, "base_path", str(tmp_path / "pkg"))
    app = FastAPI()

    github_app._mount_public_assets(app)

    assert len(_public_routes(app)) == 1
