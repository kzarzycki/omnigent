"""approvals_harness.py defines every autouse fixture of the approvals tests, and conftest.py turns each one on in
every approvals module, though the module imports none of them (#850, #1015)."""

from __future__ import annotations

import ast
from pathlib import Path

import approvals_harness
import pytest

HERE = Path(__file__).parent


def fixtures(path: Path) -> dict[str, bool]:
    """Each function a module decorates as a pytest fixture, and whether it is autouse."""
    found: dict[str, bool] = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                if ast.unparse(target) == "pytest.fixture":
                    found[node.name] = any(
                        k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value
                        for k in (call.keywords if call else [])
                    )
    return found


def harness_imports(path: Path) -> set[str]:
    """The names a module imports from approvals_harness."""
    return {
        alias.name
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module == "approvals_harness"
        for alias in node.names
    }


def test_the_harness_defines_every_autouse_fixture_and_none_is_autouse_itself() -> None:
    """AUTOUSE is what the harness defines, and each is a plain fixture, since conftest.py imports them and an
    autouse one there would stub approvals.py in every module; test_approvals.py keeps none of its own, which
    its siblings would lack."""
    defined = fixtures(HERE / "approvals_harness.py")
    assert set(approvals_harness.AUTOUSE) == set(defined)
    assert not any(defined.values())
    assert not any(fixtures(HERE / "test_approvals.py").values())
    assert set(approvals_harness.AUTOUSE) <= set(approvals_harness.__all__)


def test_no_module_of_approvals_tests_imports_an_autouse_fixture() -> None:
    modules = sorted(HERE.glob("test_approvals_*.py"))
    assert modules
    for module in modules:
        imported = set(approvals_harness.AUTOUSE) & harness_imports(module)
        assert not imported, f"{module.name} imports {sorted(imported)}"


def test_the_stubs_are_active_in_a_module_that_imports_none(request: pytest.FixtureRequest) -> None:
    """This module imports no fixture, yet board_risks has replaced board.py's GraphQL call with its stub."""
    assert set(approvals_harness.AUTOUSE) <= set(request.fixturenames)
    assert (
        approvals_harness.approvals.board()._graphql.__qualname__ == "board_risks.<locals>.graphql"
    )
