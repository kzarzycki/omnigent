#!/usr/bin/env python3
"""Deterministic supply-chain gate for an upstream delta, run before the LLM audit.

    gate.py BASE TARGET    exit 0 = pass, 1 = HOLD (reasons on stdout)
    gate.py --selftest

HOLDs only on what the lockfile and the 7-day dependency cooldown cannot cover:

- a package new to the runtime closure of `uv.lock` (what `omnigent` pulls in
  without extras or dev groups) — a new name is a new publisher to trust;
  version bumps of known packages are left to the lock and the cooldown;
- a change to code that runs at install/build time (setup.py, [build-system])
  or to the guards themselves (uv.toml, pnpm `allowBuilds` / `minimumReleaseAge`).

ponytail: new npm packages and optional-extra Python packages are not gated
(the web bundle installs frozen, behind the cooldown and with install scripts
blocked outside allowBuilds); the LLM audit still reads them. Add them here if
that proves too loose.
"""

import re
import subprocess
import sys

import tomllib

ROOT = "omnigent"


def show(rev: str, path: str) -> str:
    r = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def runtime_closure(lock_text: str) -> set[str]:
    """Package names reachable from ROOT via plain `dependencies` (no extras, no dev)."""
    if not lock_text:
        return set()
    deps: dict[str, list[str]] = {}
    for p in tomllib.loads(lock_text)["package"]:
        deps.setdefault(p["name"], []).extend(d["name"] for d in p.get("dependencies", []))
    seen, todo = set(), [ROOT]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(deps.get(name, []))
    return seen


def build_system(pyproject: str) -> str:
    m = re.search(r"^\[build-system\]\n(.*?)(?=^\[|\Z)", pyproject, re.M | re.S)
    return m.group(1) if m else ""


def pnpm_guards(workspace: str) -> str:
    m = re.search(r"^allowBuilds:\n(.*?)(?=^\S|\Z)", workspace, re.M | re.S)
    age = re.findall(r"^minimumReleaseAge:.*$", workspace, re.M)
    return (m.group(1) if m else "") + "\n".join(age)


def check(old: dict[str, str], new: dict[str, str]) -> list[str]:
    reasons = []
    added = runtime_closure(new["uv.lock"]) - runtime_closure(old["uv.lock"])
    if added:
        reasons.append(f"new runtime Python package(s): {', '.join(sorted(added))}")
    for label, fn, path in [
        ("setup.py (runs at install)", lambda t: t, "setup.py"),
        ("[build-system] in pyproject.toml", build_system, "pyproject.toml"),
        ("uv.toml (dependency cooldown)", lambda t: t, "uv.toml"),
        ("pnpm allowBuilds / minimumReleaseAge", pnpm_guards, "pnpm-workspace.yaml"),
    ]:
        if fn(old[path]) != fn(new[path]):
            reasons.append(f"changed: {label}")
    return reasons


FILES = ["uv.lock", "setup.py", "pyproject.toml", "uv.toml", "pnpm-workspace.yaml"]


def selftest() -> None:
    lock = (
        '[[package]]\nname = "omnigent"\ndependencies = [{ name = "a" }]\n'
        '[[package]]\nname = "a"\n'
    )
    base = dict.fromkeys(FILES, "") | {
        "uv.lock": lock,
        "pnpm-workspace.yaml": "allowBuilds:\n  x: true\n",
    }
    assert check(base, base) == []
    grown = (
        lock.replace('[{ name = "a" }]', '[{ name = "a" }, { name = "evil" }]')
        + '[[package]]\nname = "evil"\n'
    )
    assert check(base, base | {"uv.lock": grown}) == ["new runtime Python package(s): evil"]
    extra = (
        lock
        + '[package.optional-dependencies]\nx = [{ name = "opt" }]\n[[package]]\nname = "opt"\n'
    )
    assert check(base, base | {"uv.lock": extra}) == []
    hooks = base | {"pnpm-workspace.yaml": "allowBuilds:\n  x: true\n  evil: true\n"}
    assert check(base, hooks) == ["changed: pnpm allowBuilds / minimumReleaseAge"]
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
        sys.exit(0)
    base, target = sys.argv[1:3]
    reasons = check({f: show(base, f) for f in FILES}, {f: show(target, f) for f in FILES})
    for r in reasons:
        print(f"HOLD: {r}")
    print("gate: pass" if not reasons else "gate: HOLD")
    sys.exit(1 if reasons else 0)
