"""Report an ignore pattern that cannot compile, instead of dropping it silently."""
import copy
import io

import pytest

from pr_agent.algo.file_filter import filter_ignored, translate_globs_to_regexes
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


class _File:
    def __init__(self, filename):
        self.filename = filename


@pytest.fixture
def restore_ignore():
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("IGNORE", None))
    yield settings
    if original is None:
        settings.set("IGNORE", {})
    else:
        settings.set("IGNORE", original)


def _capture(call):
    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level="DEBUG", format="{message}", colorize=False)
    try:
        call()
    finally:
        get_logger().remove(handler_id)
    return buffer.getvalue()


def test_warn_about_an_invalid_ignore_regex(restore_ignore):
    """Warn so a typo does not silently stop excluding files."""
    restore_ignore.set("ignore.regex", ["(((|["])
    files = [_File("a.py")]

    logged = _capture(lambda: filter_ignored(files))

    assert "invalid ignore pattern" in logged.lower()


def test_a_valid_pattern_still_filters(restore_ignore):
    """A usable pattern keeps working exactly as before."""
    restore_ignore.set("ignore.regex", [r"^vendor/.*"])

    kept = filter_ignored([_File("vendor/lib.py"), _File("app.py")])

    assert [f.filename for f in kept] == ["app.py"]


def test_an_invalid_pattern_does_not_disable_the_valid_ones(restore_ignore):
    """One bad pattern must not stop the remaining patterns from applying."""
    restore_ignore.set("ignore.regex", ["(((|[", r"^vendor/.*"])

    kept = filter_ignored([_File("vendor/lib.py"), _File("app.py")])

    assert [f.filename for f in kept] == ["app.py"]


@pytest.mark.parametrize("setting", ["regex", "glob"])
@pytest.mark.parametrize("invalid", [42, False, None, ["*.py"], {"pattern": "*.py"}, b"^vendor/"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_non_string_entries_do_not_disable_valid_ignore_rules(restore_ignore, setting, invalid, invalid_first):
    regexes = [r"^vendor/"]
    globs = ["*.lock"]
    patterns = regexes if setting == "regex" else globs
    patterns.insert(0 if invalid_first else len(patterns), invalid)
    restore_ignore.set("ignore.regex", regexes)
    restore_ignore.set("ignore.glob", globs)
    files = [_File("vendor/lib.py"), _File("deps.lock"), _File("app.py")]
    result = []

    logged = _capture(lambda: result.extend(filter_ignored(files)))

    assert [f.filename for f in result] == ["app.py"]
    assert "non-string ignore" in logged.lower()
    assert "Could not filter file list" not in logged


@pytest.mark.parametrize("invalid", [42, False, None, ["*.py"], {"pattern": "*.py"}])
def test_non_string_generated_code_globs_do_not_disable_other_rules(restore_ignore, monkeypatch, invalid):
    settings = restore_ignore
    settings.set("ignore.regex", [r"^vendor/"])
    settings.set("ignore.glob", ["*.lock"])
    monkeypatch.setattr(settings.config, "ignore_language_framework", ["protobuf"])
    monkeypatch.setattr(settings.generated_code, "protobuf", [invalid, "*_pb2.py"])

    kept = filter_ignored([
        _File("vendor/lib.py"), _File("deps.lock"), _File("api_pb2.py"), _File("app.py"),
    ])

    assert [f.filename for f in kept] == ["app.py"]


def test_glob_translation_validates_before_deduplicating():
    expected = translate_globs_to_regexes(["src/**/generated.py"])
    patterns = iter([["invalid"], "src/**/generated.py", {"invalid": True}, "src/**/generated.py"])

    assert translate_globs_to_regexes(patterns) == expected
