"""approvals.py loaded by path and kept out of sys.modules: a name set on it reaches the gatekeeper code that runs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gatekeeper" / "scripts"


def load_by_path() -> object:
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("approvals_by_path", SCRIPTS / "approvals.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "approvals_by_path" not in sys.modules
    return module


def test_a_stub_set_on_a_path_loaded_shim_reaches_the_gatekeeper_code(tmp_path, capsys):
    shim = load_by_path()
    seen = []
    shim.pack_behind = lambda script: seen.append(Path(script).name) or "stale"
    shim.refuses_stale = lambda: True
    report = tmp_path / "report.md"
    report.write_text("x\n")
    assert shim.main(["report", str(report)]) == 1
    assert seen == ["approvals.py"]
    assert "stale" in capsys.readouterr().err


def test_board_reads_the_path_loaded_namespace(monkeypatch):
    shim = load_by_path()
    monkeypatch.delitem(sys.modules, "board", raising=False)
    monkeypatch.delitem(sys.modules, "approvals", raising=False)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    try:
        board = shim.board()
        assert board.one_repo is shim.one_repo
        assert sys.modules["approvals"] is shim.IMPORTER
    finally:
        sys.modules.pop("board", None)
