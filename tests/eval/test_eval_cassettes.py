"""CI gate: replay every fixture and assert the pipeline still finds what it should.

No network, no API key — reads recorded cassettes from ``fixtures/<id>/cassettes/``.
A cassette miss fails loudly (a prompt/pipeline change needs a re-record).
"""
import pytest

from tests.eval import runner
from tests.eval.fixture_provider import FIXTURES_DIR

FIXTURE_IDS = sorted(
    p.name for p in FIXTURES_DIR.iterdir()
    if p.is_dir() and (p / "expected.yaml").is_file()
)


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_fixture_replay(fixture_id):
    r = runner.run_fixture(fixture_id, mode="replay")

    if r.recall is not None:
        assert r.recall >= 0.999, (
            f"{fixture_id}: recall {r.recall:.2f}, missed {r.unmatched_must_find}")
    assert r.precision >= 0.999, (
        f"{fixture_id}: precision {r.precision:.2f}, false positives {r.false_positives}")
    assert r.count_ok, f"{fixture_id}: {r.findings_count} findings > max {r.max_findings}"

    md = r.rendered or ""
    assert md.startswith("##"), f"{fixture_id}: review markdown malformed: {md[:80]!r}"
    assert "PR Reviewer Guide" in md, f"{fixture_id}: missing reviewer guide header"


def test_at_least_one_fixture():
    assert FIXTURE_IDS, "no eval fixtures discovered"
