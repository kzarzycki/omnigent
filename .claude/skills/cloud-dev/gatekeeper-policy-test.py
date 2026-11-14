"""docs/agents/gatekeeper.md read by the engineering loop's approvals.py, with no GitHub call.

Usage: python3 gatekeeper-policy-test.py <approvals.py>

The policy parses from gatekeeper.md with no `## Settings` deprecation line; its `spec.auto`
`paths` approve a `spec:paths` spec whose PR stays in fork tooling and hold one that touches
product code (scoped(), which `check build` and `check merge` call); and the whole policy
equals POLICY, so a changed setting or glob fails. Needs PyYAML, which approvals.py imports.
"""

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GLOBS = [
    "mise.toml",
    "docs/agents/loop.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/skills/cloud-dev/**",
    ".claude/skills/fork-sync/**",
]
# the parsed policy: the settings moved from loop.md unchanged, and the globs above
POLICY = {
    "spec": {"auto": [{"paths": GLOBS}]},
    "ci": False,
    "delegate": "all",
}
INSIDE = [
    "mise.toml",
    "docs/agents/loop.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/skills/cloud-dev/SKILL.md",
    ".claude/skills/fork-sync/gate.py",
]
OUTSIDE = [  # product code, upstream's own skills and the shared hooks stay with the owner
    "omnigent/server/app.py",
    "omnigent/runtime/prompt.py",
    "web/src/App.tsx",
    "integrations/claude-cloud-acp/claude_cloud_acp.py",
    "tests/server/test_app.py",
    ".claude/skills/polly-e2e-dev/SKILL.md",
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "docs/agents.md",
    "docs/agents/gatekeeper.md",  # the policy itself: a change to it always goes to the owner
    "docs/agents/new.md",
]


def load(path: str):
    spec = importlib.util.spec_from_file_location("approvals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.writer = lambda login: login == "writer"  # no GitHub: one account can write
    return module


def labels(*names: str) -> dict:
    return {"nodes": [{"name": name} for name in names]}


def pull(*files: str, moved: dict | None = None, marks: tuple = ()) -> dict:
    nodes = [
        {"path": f, "changeType": "RENAMED" if f in (moved or {}) else "MODIFIED"} for f in files
    ]
    return {
        "number": 1,
        "labels": labels(*marks),
        "files": {"totalCount": len(nodes), "nodes": nodes},
        "previousPaths": moved or {},
    }


def issue(*marks: str, author: str = "writer") -> dict:
    return {"number": 2, "labels": labels("spec:paths", *marks), "author": {"login": author}}


def main() -> int:
    approvals = load(sys.argv[1])
    failures = []

    def expect(name: str, got, want) -> None:
        print(f"{'ok ' if got == want else 'FAIL'} {name}: {got!r}")
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    def held(name: str, why, *words: str) -> None:
        ok = why is not None and all(word in why for word in words)
        print(f"{'ok ' if ok else 'FAIL'} {name}: held, {why}")
        if not ok:
            failures.append(f"{name}: {why!r} lacks {words}")

    policy = (ROOT / "docs/agents/gatekeeper.md").read_text()
    loop = (ROOT / "docs/agents/loop.md").read_text()
    expect(
        "loop.md holds no settings block",
        bool(approvals.SETTINGS.search(approvals.section(loop, "Settings"))),
        False,
    )
    err = io.StringIO()
    os.chdir(ROOT)  # project() reads the checkout it runs in
    with contextlib.redirect_stderr(err):
        _, read = approvals.project()
    expect("project() reads gatekeeper.md", approvals.source(read), "gatekeeper.md")
    expect("no `## Settings` deprecation line", "## Settings" in err.getvalue(), False)
    settings = approvals.settings(approvals.Policy(policy))
    expect("spec.auto globs", approvals.spec_paths(read), GLOBS)
    expect("spec.auto holds no other kind", approvals.spec_kinds(read), set())
    expect("spec.auto is not true", approvals.spec_auto(read), False)
    expect("delegate", approvals.delegation(read), "all")

    expect("no land section: PRs land as GitHub merges", "land" in settings, False)
    expect("the whole policy", settings, POLICY)

    expect("check build, inside the globs", approvals.scoped(pull(*INSIDE), issue(), read), None)
    for path in OUTSIDE:
        held(
            f"check build, plus {path}",
            approvals.scoped(pull(*INSIDE, path), issue(), read),
            f"`{path}`",
            "outside",
        )
    held(
        "rename from product code into the globs",
        approvals.scoped(
            pull("docs/agents/loop.md", moved={"docs/agents/loop.md": "omnigent/x.py"}),
            issue(),
            read,
        ),
        "`omnigent/x.py`",
    )
    held(
        "risk:high on the issue",
        approvals.scoped(pull("mise.toml"), issue("risk:high"), read),
        "risk:high",
    )
    held(
        "risk:high on the PR",
        approvals.scoped(pull("mise.toml", marks=("risk:high",)), issue(), read),
        "risk:high",
    )
    held(
        "author who can't write",
        approvals.scoped(pull("mise.toml"), issue(author="outsider"), read),
        "@outsider",
    )

    print(f"gatekeeper policy: {'ok' if not failures else f'{len(failures)} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
