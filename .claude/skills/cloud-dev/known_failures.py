"""pytest plugin: run the tests in known_failures.txt as non-strict xfail.

Load with `-p known_failures` and PYTHONPATH naming this directory (run-affected-tests.sh does).
A line is `<test id><TAB>#<issue><TAB><reason>`; one lacking an issue or a reason is refused.
"""

import re
from pathlib import Path

import pytest

LIST = Path(__file__).with_name("known_failures.txt")


def load(path: Path = LIST) -> dict[str, str]:
    """Test id -> xfail reason, from the list's lines."""
    known: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not re.fullmatch(r"#\d+", parts[1]) or not parts[2].strip():
            raise pytest.UsageError(
                f"{path.name}:{number}: want <test id><TAB>#<issue><TAB><reason>"
            )
        known[parts[0]] = f"known failure {parts[1]}: {parts[2]}"
    return known


def pytest_collection_modifyitems(config, items):
    known = load(Path(config.getoption("--known-failures-file") or LIST))
    for item in items:
        if item.nodeid in known:
            item.add_marker(pytest.mark.xfail(reason=known[item.nodeid], strict=False))


def pytest_addoption(parser):
    parser.addoption("--known-failures-file", default=None, help="the known-failures list to use")
