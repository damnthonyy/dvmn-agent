"""Parse a multi-file unified diff (a ``git diff`` / ``.patch``) into ``FilePatchInfo``.

There is no generic diff parser in ``pr_agent/algo/`` — every git provider ships its own.
This mirrors the split idiom in ``pr_agent/git_providers/bitbucket_provider.py`` (split on
``diff --git``) and produces the same ``FilePatchInfo`` shape the pipeline consumes.

``base_file`` / ``head_file`` are only reconstructed for pure additions / deletions;
for modifications they stay ``""``. ``extend_patch`` tolerates an empty original file
(it just skips context expansion), so fixture ``diff.patch`` files should be authored
with wide context (``git diff -U15``).
"""
from __future__ import annotations

import re

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

_DIFF_GIT_RE = re.compile(r"^diff --git ", re.MULTILINE)
_OLD_PATH_RE = re.compile(r"^--- (?:a/)?(.+)$", re.MULTILINE)
_NEW_PATH_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ ", re.MULTILINE)


def _filename(block: str) -> tuple[str, str | None]:
    """Return (new_path, old_path_if_renamed_or_deleted)."""
    header = block.split("\n", 1)[0]
    m = re.match(r"diff --git a/(.+?) b/(.+)$", header)
    if m:
        a, b = m.group(1), m.group(2)
        return (b, a if a != b else None)
    # fall back to the ---/+++ lines
    old = _OLD_PATH_RE.search(block)
    new = _NEW_PATH_RE.search(block)
    new_p = new.group(1) if new else "unknown"
    if new_p == "/dev/null" and old:
        return (old.group(1), None)
    old_p = old.group(1) if old else None
    return (new_p, old_p if old_p and old_p != new_p and old_p != "/dev/null" else None)


def _edit_type(block: str) -> EDIT_TYPE:
    if "\nnew file mode " in block or "\n+++ b/dev/null" in block:
        return EDIT_TYPE.ADDED
    if "\ndeleted file mode " in block or re.search(r"^\+\+\+ /dev/null$", block, re.MULTILINE):
        return EDIT_TYPE.DELETED
    if "\nrename from " in block:
        return EDIT_TYPE.RENAMED
    return EDIT_TYPE.MODIFIED


def _added_lines(block: str) -> str:
    out = []
    for line in block.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out) + ("\n" if out else "")


def _removed_lines(block: str) -> str:
    out = []
    for line in block.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            out.append(line[1:])
    return "\n".join(out) + ("\n" if out else "")


def parse(diff_text: str) -> list[FilePatchInfo]:
    """Split *diff_text* into one ``FilePatchInfo`` per changed file."""
    if not diff_text.strip():
        return []

    # locate each "diff --git" header and slice up to the next one
    starts = [m.start() for m in _DIFF_GIT_RE.finditer(diff_text)]
    if not starts:
        # a bare single-file unified diff with no "diff --git" line
        starts = [0]
    blocks = [diff_text[s: (starts[i + 1] if i + 1 < len(starts) else len(diff_text))]
              for i, s in enumerate(starts)]

    files: list[FilePatchInfo] = []
    for block in blocks:
        if not _HUNK_RE.search(block):
            continue  # pure mode change / binary / empty — nothing to review
        new_path, old_path = _filename(block)
        edit = _edit_type(block)
        hunk_start = _HUNK_RE.search(block).start()
        patch = block[hunk_start:]
        base_file = _removed_lines(patch) if edit is EDIT_TYPE.DELETED else ""
        head_file = _added_lines(patch) if edit is EDIT_TYPE.ADDED else ""
        num_plus = sum(1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        num_minus = sum(1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        files.append(FilePatchInfo(
            base_file=base_file,
            head_file=head_file,
            patch=patch if patch.endswith("\n") else patch + "\n",
            filename=new_path,
            edit_type=edit,
            old_filename=old_path,
            num_plus_lines=num_plus,
            num_minus_lines=num_minus,
        ))
    return files
