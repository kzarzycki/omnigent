"""known_failures.py in a temporary pytest project: listed tests run as xfail, a bad line is refused.

Usage: python3 known-failures-test.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest_run(project: Path, listing: str) -> subprocess.CompletedProcess:
    (project / "list.txt").write_text(listing)
    (project / "test_sample.py").write_text(
        "import pytest\n\ndef test_known_bad():\n    assert 0\n\ndef test_new_bad():\n    assert 0\n"
        "\ndef test_fine():\n    pass\n\n@pytest.mark.parametrize('x', ['a b'])\ndef test_param(x):\n    assert 0\n"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "known_failures", "-p", "no:cacheprovider",
         "--known-failures-file", str(project / "list.txt"), str(project / "test_sample.py")],
        capture_output=True, text=True, cwd=project, env={"PYTHONPATH": str(HERE), "PATH": ""},
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        # ids are relative to pytest's rootdir, which is the temp project
        ok = "test_sample.py::test_known_bad\t#1\twhy\ntest_sample.py::test_param[a b]\t#2\twhy\n"
        run = pytest_run(project, "# c\n\n" + ok)
        assert "2 xfailed" in run.stdout and "1 failed" in run.stdout and run.returncode == 1, run.stdout
        both = pytest_run(project, ok + "test_sample.py::test_new_bad\t#3\twhy\n")
        assert "3 xfailed" in both.stdout and both.returncode == 0, both.stdout
        for bad in ("test_sample.py::test_known_bad\t#1\n", "test_sample.py::test_known_bad\t1\twhy\n"):
            refused = pytest_run(project, bad)
            assert refused.returncode != 0 and "list.txt:1" in refused.stdout + refused.stderr, refused.stdout + refused.stderr
    shipped = (HERE / "known_failures.txt").read_text().splitlines()
    sys.path.insert(0, str(HERE))
    import known_failures

    entries = known_failures.load()
    assert len(entries) == sum(1 for line in shipped if line and not line.startswith("#"))
    print("cloud-dev known_failures.py: ok")


main()
