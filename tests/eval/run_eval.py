"""CLI: run the eval golden set.

    python -m tests.eval.run_eval                     # replay all fixtures, print a table
    python -m tests.eval.run_eval --only sqli-001     # one fixture
    python -m tests.eval.run_eval --record --only X   # (re)record cassettes (needs OPENAI_KEY)
    python -m tests.eval.run_eval --live --json out.json --check-baseline
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tests.eval import runner
from tests.eval.fixture_provider import FIXTURES_DIR

BASELINE = Path(__file__).parent / "BASELINE.json"


def discover(only: list[str] | None) -> list[str]:
    ids = sorted(p.name for p in FIXTURES_DIR.iterdir()
                 if p.is_dir() and (p / "expected.yaml").is_file())
    if only:
        ids = [i for i in ids if i in only]
    return ids


def _fmt(v, spec="{:.2f}"):
    return "  n/a" if v is None else spec.format(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run_eval")
    ap.add_argument("--only", nargs="*", help="fixture ids to run")
    ap.add_argument("--record", action="store_true", help="write cassettes from real calls")
    ap.add_argument("--live", action="store_true", help="real calls, no cassette write")
    ap.add_argument("--json", type=Path, help="write full results as JSON")
    ap.add_argument("--check-baseline", action="store_true",
                    help="fail if recall drops or precision drops >0.05 vs BASELINE.json")
    args = ap.parse_args(argv)

    mode = "record" if args.record else "live" if args.live else "replay"
    ids = discover(args.only)
    if not ids:
        print("no fixtures found", file=sys.stderr)
        return 2

    results = []
    print(f"mode={mode}  fixtures={len(ids)}\n")
    header = f"{'fixture':<20} {'recall':>7} {'prec':>6} {'find':>5} {'cnt':>4} {'cost$':>8} {'corr':>6}  {'ok'}"
    print(header)
    print("-" * len(header))
    for fid in ids:
        r = runner.run_fixture(fid, mode=mode)
        results.append(r)
        print(f"{fid:<20} {_fmt(r.recall):>7} {_fmt(r.precision):>6} "
              f"{r.findings_count:>5} {('ok' if r.count_ok else 'OVER'):>4} "
              f"{_fmt(r.cost_usd, '{:.4f}'):>8} {_fmt(r.corroboration_rate):>6}  "
              f"{'PASS' if r.passed else 'FAIL'}")
        for mf in r.unmatched_must_find:
            print(f"    ! missed: {mf}")
        for fp in r.false_positives:
            print(f"    ! false positive: {fp[:100]}")

    agg = aggregate(results)
    print("\naggregate:", json.dumps(agg, indent=2))

    if args.json:
        args.json.write_text(json.dumps(
            {"mode": mode, "aggregate": agg,
             "fixtures": [_result_dict(r) for r in results]}, indent=2) + "\n")
        print(f"wrote {args.json}")

    exit_code = 0 if all(r.passed for r in results) else 1

    if args.check_baseline:
        exit_code = max(exit_code, _check_baseline(results))

    return exit_code


def _result_dict(r) -> dict:
    return {"fixture_id": r.fixture_id, "recall": r.recall, "precision": r.precision,
            "findings_count": r.findings_count, "count_ok": r.count_ok,
            "cost_usd": r.cost_usd, "cost_ok": r.cost_ok,
            "corroboration_rate": r.corroboration_rate, "elapsed_ms": r.elapsed_ms,
            "passed": r.passed}


def aggregate(results) -> dict:
    recalls = [r.recall for r in results if r.recall is not None]
    return {
        "n": len(results),
        "passed": sum(1 for r in results if r.passed),
        "mean_recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "mean_precision": round(sum(r.precision for r in results) / len(results), 4),
        "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
        "total_findings": sum(r.findings_count for r in results),
    }


def _check_baseline(results) -> int:
    if not BASELINE.is_file():
        print(f"\n--check-baseline: no {BASELINE.name} yet (skipped)")
        return 0
    base = json.loads(BASELINE.read_text())
    by_id = {b["fixture_id"]: b for b in base.get("fixtures", [])}
    regressed = False
    for r in results:
        b = by_id.get(r.fixture_id)
        if not b:
            continue
        if r.recall is not None and b.get("recall") is not None and r.recall < b["recall"] - 1e-6:
            print(f"\nREGRESSION {r.fixture_id}: recall {b['recall']:.2f} -> {r.recall:.2f}")
            regressed = True
        if r.precision < b.get("precision", 0) - 0.05:
            print(f"\nREGRESSION {r.fixture_id}: precision {b['precision']:.2f} -> {r.precision:.2f}")
            regressed = True
    print("\n--check-baseline:", "REGRESSED" if regressed else "ok")
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
