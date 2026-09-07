"""An in-memory ``GitProvider`` backed by a ``tests/eval/fixtures/<id>/`` directory.

Only what ``/review`` touches is implemented for real; every publish/mutation method is a
no-op and everything else raises. Registered into ``pr_agent.git_providers._GIT_PROVIDERS``
by :mod:`tests.eval.runner`, selected via ``config.git_provider = "fixture"``.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import List

import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.git_providers.git_provider import GitProvider
from tests.eval import patch_parser

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# extension -> GitHub "linguist" language name, enough for get_main_pr_language()
_EXT_LANG = {
    "py": "Python", "js": "JavaScript", "jsx": "JavaScript", "ts": "TypeScript",
    "tsx": "TypeScript", "tf": "HCL", "hcl": "HCL", "go": "Go", "rb": "Ruby",
    "java": "Java", "php": "PHP", "sh": "Shell", "yaml": "YAML", "yml": "YAML",
    "sql": "SQL", "md": "Markdown", "json": "JSON",
}


class PullRequestMimic:
    """Mimics the PyGithub PullRequest surface the pipeline reads (title only)."""

    def __init__(self, title: str, diff_files: List[FilePatchInfo]):
        self.title = title
        self.diff_files = diff_files


class FixtureGitProvider(GitProvider):
    def __init__(self, fixture_id: str, incremental=False):
        self.fixture_id = fixture_id
        self.fixture_dir = (fixture_id if os.path.isdir(str(fixture_id))
                            else FIXTURES_DIR / str(fixture_id))
        self.fixture_dir = Path(self.fixture_dir)
        if not self.fixture_dir.is_dir():
            raise FileNotFoundError(f"eval fixture not found: {self.fixture_dir}")

        self.expected = yaml.safe_load((self.fixture_dir / "expected.yaml").read_text()) or {}
        diff_text = (self.fixture_dir / "diff.patch").read_text()
        self.diff_files: List[FilePatchInfo] = patch_parser.parse(diff_text)

        title = (self.expected.get("meta") or {}).get("title") or f"fixture {self.fixture_id}"
        self.pr = PullRequestMimic(title, self.diff_files)
        self.incremental = incremental
        self.published_labels: list[str] = []
        self.published_comments: list[str] = []

    # --- inputs the review pipeline actually reads -------------------------------
    def get_diff_files(self) -> list[FilePatchInfo]:
        return self.diff_files

    def get_files(self) -> list[str]:
        return [f.filename for f in self.diff_files]

    def get_languages(self) -> dict:
        counts = Counter()
        for f in self.diff_files:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            counts[_EXT_LANG.get(ext, ext or "Text")] += 1
        total = sum(counts.values()) or 1
        return {lang: 100.0 * n / total for lang, n in counts.items()}

    def get_pr_branch(self) -> str:
        return f"fixture/{self.fixture_id}"

    def get_pr_description_full(self) -> str:
        return (self.expected.get("meta") or {}).get("description", "") or ""

    def get_commit_messages(self) -> str:
        return (self.expected.get("meta") or {}).get("commit_messages", "") or ""

    def get_repo_settings(self):
        return None  # keeps apply_repo_settings() fully offline

    def get_user_id(self):
        return "eval-fixture"

    def is_supported(self, capability: str) -> bool:
        # gfm_markdown True -> exercises the fork's _convert_to_markdown_compact path
        if capability in ("get_issue_comments", "create_inline_comment",
                          "publish_inline_comments", "get_labels"):
            return False
        return True

    def get_issue_comments(self):
        return []

    def get_pr_labels(self, update=False):
        return []

    def get_pr_id(self):
        return self.fixture_id

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        return ""

    # --- publish / mutation surface: all inert ---------------------------------
    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not is_temporary:
            self.published_comments.append(pr_comment)

    def publish_persistent_comment(self, pr_comment: str, *a, **kw):
        self.published_comments.append(pr_comment)

    def publish_description(self, pr_title: str, pr_body: str):
        pass

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        return True

    def publish_labels(self, labels):
        self.published_labels = list(labels or [])

    def publish_inline_comment(self, body, relevant_file, relevant_line_in_file, original_suggestion=None):
        pass

    def publish_inline_comments(self, comments: list):
        pass

    def remove_initial_comment(self):
        pass

    def remove_comment(self, comment):
        pass

    def add_eyes_reaction(self, issue_comment_id, disable_eyes: bool = False):
        return None

    def remove_reaction(self, issue_comment_id, reaction_id):
        return None

    # --- clone plumbing: never used offline ----------------------------------
    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        raise NotImplementedError("FixtureGitProvider is offline-only")

    def get_canonical_url_parts(self, repo_git_url: str, desired_branch: str):
        raise NotImplementedError("FixtureGitProvider is offline-only")
