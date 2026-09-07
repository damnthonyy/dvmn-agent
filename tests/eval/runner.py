"""Run ``/review`` against one fixture and return a scored :class:`FixtureResult`.

Drives the real ``PRReviewer`` pipeline; the only seams are the provider registry entry,
the injected AI handler, and a tee on ``convert_to_markdown_v2`` to capture the parsed
review dict (which is otherwise local to ``PRReviewer._prepare_pr_review``).

Modes: ``replay`` (default, no network), ``record`` (writes cassettes), ``live``.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import yaml
from starlette_context import context, request_cycle_context

from tests.eval import cassette, scorer
from tests.eval.fixture_provider import FIXTURES_DIR, FixtureGitProvider


def _apply_settings():
    from pr_agent.config_loader import get_settings
    s = get_settings()
    s.set("config.git_provider", "fixture")
    s.set("config.publish_output", False)
    s.set("config.publish_output_progress", False)
    s.set("config.use_repo_settings_file", False)
    s.set("config.fallback_models", [])
    s.set("config.is_auto_command", False)
    s.set("pr_reviewer.require_ticket_analysis_review", False)
    s.set("pr_reviewer.enable_review_labels_security", False)
    s.set("pr_reviewer.enable_review_labels_effort", False)
    s.set("pr_reviewer.persistent_comment", False)


def _register_provider():
    from pr_agent.git_providers import _GIT_PROVIDERS
    _GIT_PROVIDERS["fixture"] = FixtureGitProvider


class _TeeAIHandler:
    """Wrap a real handler; record (model, system, user, resp, finish_reason) per call."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []
        self.main_pr_language = ""

    @property
    def deployment_id(self):
        return getattr(self.inner, "deployment_id", None)

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        resp, finish_reason = await self.inner.chat_completion(
            model=model, system=system, user=user, temperature=temperature, img_path=img_path)
        self.calls.append({"model": model, "system": system, "user": user,
                           "resp": resp, "finish_reason": finish_reason})
        return resp, finish_reason


def load_expected(fixture_dir: Path) -> dict:
    return yaml.safe_load((Path(fixture_dir) / "expected.yaml").read_text()) or {}


def fixture_dir_for(fixture_id: str) -> Path:
    p = Path(fixture_id)
    return p if p.is_dir() else FIXTURES_DIR / fixture_id


def run_fixture(fixture_id: str, mode: str = "replay") -> scorer.FixtureResult:
    fixture_dir = fixture_dir_for(fixture_id)
    expected = load_expected(fixture_dir)
    expected["_id"] = fixture_dir.name

    _register_provider()
    captured: dict = {}

    def _tee_markdown(output_data, *a, **kw):
        captured["data"] = copy.deepcopy(output_data)
        return _tee_markdown.real(output_data, *a, **kw)

    with request_cycle_context({}):
        from pr_agent.config_loader import global_settings, get_settings
        context["settings"] = copy.deepcopy(global_settings)
        _apply_settings()

        import pr_agent.tools.pr_reviewer as prr
        _tee_markdown.real = prr.convert_to_markdown_v2
        prr.convert_to_markdown_v2 = _tee_markdown

        cost_usd = elapsed_ms = 0.0
        try:
            if mode == "replay":
                handler = cassette.ReplayAIHandler(fixture_dir, tool="review")
                _run_review(prr, fixture_dir.name, handler)
                cost_usd, elapsed_ms = handler.total_cost_usd, handler.total_elapsed_ms
            else:
                from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
                tee = _TeeAIHandler(LiteLLMAIHandler())
                with cassette.recording_patch() as meta_calls:
                    _run_review(prr, fixture_dir.name, tee)
                for i, call in enumerate(tee.calls):
                    meta = meta_calls[i] if i < len(meta_calls) else {}
                    cost_usd += float(meta.get("cost_usd") or 0.0)
                    elapsed_ms += float(meta.get("elapsed_ms") or 0.0)
                    if mode == "record":
                        cassette.write_cassette(
                            fixture_dir, "review", i, model=call["model"],
                            system=call["system"], user=call["user"],
                            resp=call["resp"], finish_reason=call["finish_reason"], meta=meta)
        finally:
            prr.convert_to_markdown_v2 = _tee_markdown.real

        rendered = (get_settings().get("data", {}) or {}).get("artifact", "")

    if "data" not in captured and rendered == "":
        if mode == "replay" and not list((fixture_dir / "cassettes").glob("review.*.json")):
            raise cassette.CassetteMiss(
                f"{expected['_id']}: no cassettes under {fixture_dir.name}/cassettes/ — "
                f"run: python -m tests.eval.run_eval --record --only {expected['_id']}")
        raise RuntimeError(
            f"{expected['_id']}: /review produced no output — the pipeline raised "
            f"(PRReviewer.run swallows exceptions; check the logged traceback) or the "
            f"cassette response failed to parse.")

    findings = scorer.normalize_findings(captured.get("data", {}))
    result = scorer.score(expected, findings, cost_usd, elapsed_ms)
    result.rendered = rendered
    return result


def _run_review(prr_module, fixture_id: str, handler):
    reviewer = prr_module.PRReviewer(fixture_id, ai_handler=lambda: handler)
    asyncio.run(reviewer.run())
