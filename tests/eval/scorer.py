"""Normalise ``/review`` output into findings and score them against ``expected.yaml``.

``normalize_findings`` is the *single* coupling point to today's output shape. When A2
(the ``Finding`` object) lands, swap its body to read ``list[Finding]`` and nothing else
in the harness changes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

_LINE_TOLERANCE = 5

# issue_header (lowercased) -> coarse category
_HEADER_CATEGORY = {
    "possible bug": "correctness",
    "possible issue": "correctness",
    "bug": "correctness",
    "logic error": "correctness",
    "performance": "performance",
    "security": "security",
    "vulnerability": "security",
    "data leak": "security",
    "typo": "style",
    "style": "style",
    "naming": "style",
}
_CWE_RE = re.compile(r"CWE[- ]?(\d+)", re.IGNORECASE)


@dataclass
class Finding:
    file: str
    start_line: Optional[int]
    end_line: Optional[int]
    category: str
    severity: Optional[str]
    cwe: Optional[str]
    source: str  # "llm" | "scanner" | "both"
    text: str = ""


def _categorize(header: str, text: str) -> str:
    h = (header or "").strip().lower()
    if h in _HEADER_CATEGORY:
        return _HEADER_CATEGORY[h]
    blob = f"{h} {text}".lower()
    if any(w in blob for w in ("sql injection", "xss", "csrf", "secret", "hardcoded",
                               "credential", "auth", "injection", "ssrf", "path traversal",
                               "deserial", "rce", "vulnerab")):
        return "security"
    if any(w in blob for w in ("race", "null", "off-by-one", "crash", "exception", "leak")):
        return "correctness"
    return "other"


def normalize_findings(review_data: dict) -> list[Finding]:
    review = (review_data or {}).get("review", {}) or {}
    findings: list[Finding] = []

    for item in review.get("key_issues_to_review", []) or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("issue_content", ""))
        header = str(item.get("issue_header", ""))
        cwe_m = _CWE_RE.search(f"{header} {text}")
        findings.append(Finding(
            file=str(item.get("relevant_file", "")).strip(),
            start_line=_as_int(item.get("start_line")),
            end_line=_as_int(item.get("end_line")),
            category=_categorize(header, text),
            severity=None,
            cwe=f"CWE-{cwe_m.group(1)}" if cwe_m else None,
            source="llm",
            text=f"{header}: {text}"[:400],
        ))

    sec = review.get("security_concerns")
    if isinstance(sec, str) and sec.strip() and not _is_no(sec):
        cwe_m = _CWE_RE.search(sec)
        findings.append(Finding(
            file=_first_path(sec), start_line=None, end_line=None,
            category="security", severity="high",
            cwe=f"CWE-{cwe_m.group(1)}" if cwe_m else None,
            source="llm", text=f"security_concerns: {sec}"[:400],
        ))
    return findings


@dataclass
class FixtureResult:
    fixture_id: str
    recall: Optional[float]
    precision: float
    findings_count: int
    max_findings: int
    count_ok: bool
    cost_usd: float
    max_cost_usd: Optional[float]
    cost_ok: bool
    corroboration_rate: Optional[float]
    unmatched_must_find: list = field(default_factory=list)
    false_positives: list = field(default_factory=list)
    elapsed_ms: float = 0.0
    rendered: str = ""

    @property
    def passed(self) -> bool:
        ok = self.precision >= 0.999 and self.count_ok and self.cost_ok
        if self.recall is not None:
            ok = ok and self.recall >= 0.999
        return ok


def _match(expect: dict, f: Finding) -> bool:
    if os.path.basename(expect["file"]) != os.path.basename(f.file or ""):
        return False
    hint = expect.get("line_hint")
    if hint is not None and f.start_line is not None:
        lo, hi = f.start_line, f.end_line or f.start_line
        if not (lo - _LINE_TOLERANCE <= hint <= hi + _LINE_TOLERANCE):
            return False
    if expect.get("category") and f.category and expect["category"] != f.category:
        return False
    if expect.get("cwe") and f.cwe and expect["cwe"].upper() != f.cwe.upper():
        return False
    return True


def score(expected: dict, findings: list[Finding], cost_usd: float, elapsed_ms: float = 0.0) -> FixtureResult:
    must_find = expected.get("must_find", []) or []
    must_not = expected.get("must_not_find", []) or []
    max_findings = int(expected.get("max_findings", 10))
    max_cost = expected.get("max_cost_usd")

    unmatched = [mf for mf in must_find if not any(_match(mf, f) for f in findings)]
    recall = None if not must_find else (len(must_find) - len(unmatched)) / len(must_find)

    fp = []
    for f in findings:
        if not must_find and not must_not:
            fp.append(f.text or f.file)  # clean fixture: any finding is a false positive
            continue
        for mn in must_not:
            if mn.get("file") and os.path.basename(mn["file"]) == os.path.basename(f.file or ""):
                fp.append(f.text or f.file)
                break
            if mn.get("category") and mn["category"] == f.category:
                fp.append(f.text or f.file)
                break
    total = len(findings)
    precision = 1.0 if total == 0 else max(0.0, 1.0 - len(fp) / total)

    corr = None
    if findings and any(f.source == "both" for f in findings):
        corr = sum(1 for f in findings if f.source == "both") / len(findings)

    return FixtureResult(
        fixture_id=expected.get("_id", "?"),
        recall=recall,
        precision=precision,
        findings_count=total,
        max_findings=max_findings,
        count_ok=total <= max_findings,
        cost_usd=round(cost_usd, 4),
        max_cost_usd=max_cost,
        cost_ok=(max_cost is None) or (cost_usd <= float(max_cost)),
        corroboration_rate=corr,
        unmatched_must_find=[mf.get("file") or mf.get("cwe") or str(mf) for mf in unmatched],
        false_positives=fp,
        elapsed_ms=round(elapsed_ms, 1),
    )


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _is_no(s: str) -> bool:
    return s.strip().lower() in ("no", "none", "n/a", "no.", "no issues", "no security concerns")


def _first_path(s: str):
    m = re.search(r"[\w./-]+\.\w{1,4}", s or "")
    return m.group(0) if m else ""
