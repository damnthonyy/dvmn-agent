"""Eval / golden-set harness for the review pipeline (issue #29 / P4).

Runs ``/review`` against fixture PRs under ``tests/eval/fixtures/<id>/`` and scores
the findings against declarative expectations. Two modes:

* **replay** (default, CI): reads recorded LLM responses from ``<fixture>/cassettes/``.
  No network, no API key, deterministic. Tests pipeline composition + parsing + rendering.
* **live** (``--live``, nightly): real LLM calls. Measures prompt/model quality, cost, latency.

Nothing in ``pr_agent/`` is modified; the harness registers a ``FixtureGitProvider``
into the provider registry at runtime and injects a cassette-backed AI handler.
"""
