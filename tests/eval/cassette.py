"""Record / replay layer for LLM calls in the eval harness.

Keyed positionally: ``<fixture>/cassettes/<tool>.<call_index>.json``. ``/review`` makes
exactly one ``chat_completion`` call, so a fixture normally has ``review.0.json``.
Positional keys are date-independent (the review prompt embeds ``datetime.now()``), and a
prompt edit still replays the recorded response — cassette mode tests the *pipeline*
(compose → parse → render → score); the nightly ``--live`` run measures model quality.

* **replay** (default): :class:`ReplayAIHandler`, a plain ``BaseAiHandler``. No litellm,
  no network. Cost/latency come from the cassette's ``meta``.
* **record** (``--record``): :func:`recording_patch` wraps ``litellm.acompletion`` to
  capture the raw response + ``completion_cost`` while the real ``LiteLLMAIHandler`` runs
  unchanged; the runner writes the cassette afterwards.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler


def cassette_path(fixture_dir: Path, tool: str, index: int) -> Path:
    return Path(fixture_dir) / "cassettes" / f"{tool}.{index}.json"


class CassetteMiss(RuntimeError):
    pass


class ReplayAIHandler(BaseAiHandler):
    """Returns recorded ``(resp, finish_reason)`` tuples by call order."""

    def __init__(self, fixture_dir: Path, tool: str = "review"):
        self.fixture_dir = Path(fixture_dir)
        self.tool = tool
        self._i = 0
        self.calls: list[dict] = []  # {cost_usd, elapsed_ms, prompt_tokens, completion_tokens}
        self.main_pr_language = ""

    @property
    def deployment_id(self):
        return None

    async def chat_completion(self, model: str, system: str, user: str,
                              temperature: float = 0.2, img_path: Optional[str] = None):
        path = cassette_path(self.fixture_dir, self.tool, self._i)
        if not path.is_file():
            raise CassetteMiss(
                f"no cassette {path.relative_to(self.fixture_dir.parent.parent)} — "
                f"run: python -m tests.eval.run_eval --record --only {self.fixture_dir.name}")
        data = json.loads(path.read_text())
        self._i += 1
        self.calls.append(data.get("meta", {}))
        r = data["response"]
        return r["resp"], r.get("finish_reason", "stop")

    @property
    def total_cost_usd(self) -> float:
        return sum(float(c.get("cost_usd") or 0.0) for c in self.calls)

    @property
    def total_elapsed_ms(self) -> float:
        return sum(float(c.get("elapsed_ms") or 0.0) for c in self.calls)


@contextmanager
def recording_patch():
    """Patch ``litellm.acompletion`` to stash (response, cost, tokens, elapsed) per call.

    Yields a list that fills with one dict per completion. The real
    ``LiteLLMAIHandler.chat_completion`` runs untouched and still returns its
    ``(resp, finish_reason)`` tuple.
    """
    import litellm

    from pr_agent.algo.ai_handlers import litellm_ai_handler as mod

    captured: list[dict] = []
    real = mod.acompletion

    async def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        resp = await real(*args, **kwargs)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        entry = {"elapsed_ms": round(elapsed_ms, 1)}
        try:
            entry["cost_usd"] = float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            entry["cost_usd"] = None
        try:
            usage = getattr(resp, "usage", None) or resp["usage"]
            entry["prompt_tokens"] = getattr(usage, "prompt_tokens", None) or usage.get("prompt_tokens")
            entry["completion_tokens"] = getattr(usage, "completion_tokens", None) or usage.get("completion_tokens")
        except Exception:
            pass
        captured.append(entry)
        return resp

    mod.acompletion = wrapper
    try:
        yield captured
    finally:
        mod.acompletion = real


def write_cassette(fixture_dir: Path, tool: str, index: int, *, model: str, system: str,
                   user: str, resp: str, finish_reason: str, meta: dict) -> Path:
    path = cassette_path(fixture_dir, tool, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "request": {"model": model, "system": system, "user": user},
        "response": {"resp": resp, "finish_reason": finish_reason},
        "meta": meta,
    }, indent=2, ensure_ascii=False) + "\n")
    return path
