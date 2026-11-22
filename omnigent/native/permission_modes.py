"""Native permission modes accepted by automations and their launch arguments."""

# Keep these mappings aligned with the web permission constants in
# claudePermissionMode.ts and nativeHarnessModes.ts.
PERMISSION_LAUNCH_ARGS: dict[str, dict[str, list[str]]] = {
    "claude-native": {
        "default": ["--permission-mode", "default"],
        "auto": ["--permission-mode", "auto"],
        "acceptEdits": ["--permission-mode", "acceptEdits"],
        "plan": ["--permission-mode", "plan"],
        "dontAsk": ["--permission-mode", "dontAsk"],
        "bypassPermissions": ["--permission-mode", "bypassPermissions"],
    },
    "codex-native": {
        "default": [],
        "ask-for-approval": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="user"',
        ],
        "approve-for-me": [
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "on-request",
            "-c",
            'approvals_reviewer="auto_review"',
        ],
        "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
        "bypass": [],
    },
    "cursor-native": {
        "default": [],
        "auto-review": ["--auto-review"],
        "plan": ["--mode", "plan"],
        "ask": ["--mode", "ask"],
        "yolo": ["--yolo"],
    },
    "antigravity-native": {"default": [], "skip": ["--dangerously-skip-permissions"]},
    "devin-native": {
        "normal": [],
        "accept-edits": ["--permission-mode", "accept-edits"],
        "smart": ["--permission-mode", "smart"],
        "dangerous": ["--permission-mode", "dangerous"],
    },
}

# Shared by every rejection of an automation's permission_mode.
AGENT_DEFAULT_PERMISSION_MODE_HINT = "JSON null (not the string 'null') for the agent default"

# Modes that skip the approval prompt (``dontAsk`` denies instead, by rules the server
# cannot see). An automation runs in one only when the task itself selects it, never
# through an agent default or a reused session's live mode.
APPROVAL_SKIPPING_PERMISSION_MODES: frozenset[str] = frozenset(
    {
        "bypassPermissions",  # claude-native, claude-sdk, acp
        "dontAsk",  # claude-native
        "full-access",  # codex-native
        "bypass",  # codex-native
        "yolo",  # cursor-native
        "skip",  # antigravity-native
        "dangerous",  # devin-native
    }
)


# Modes of each harness from strictest to loosest. A request may name a mode only when it is
# no looser than the agent's default. Positions are a fixed policy, not a measurement:
# a mode that asks the user before acting ranks stricter than one that decides alone.
PERMISSION_MODE_STRICTNESS: dict[str, tuple[str, ...]] = {
    "claude-native": ("plan", "default", "acceptEdits", "auto", "dontAsk", "bypassPermissions"),
    "codex-native": (
        "read-only",
        "ask-for-approval",
        "default",
        "approve-for-me",
        "full-access",
        "bypass",
    ),
    "cursor-native": ("ask", "plan", "default", "auto-review", "yolo"),
    "antigravity-native": ("default", "skip"),
    "devin-native": ("normal", "accept-edits", "smart", "dangerous"),
    "acp": ("auto", "bypassPermissions"),
}

# Launch args that stand for a mode whose table entry carries no args.
_BYPASS_LAUNCH_ARGS: dict[str, tuple[tuple[str, ...], str]] = {
    "codex-native": (("--dangerously-bypass-approvals-and-sandbox",), "bypass"),
}


def mode_for_launch_args(harness: str | None, args: list[str] | None) -> str | None:
    """
    Name the mode that *args* launch *harness* in, for comparing against a requested mode.

    :param harness: Canonical harness, e.g. ``"codex-native"``.
    :param args: Launch args derived for the session, or ``None`` for the harness's own stance.
    :returns: The mode, or ``None`` when *args* match no mode of the harness.
    """
    modes = PERMISSION_LAUNCH_ARGS.get(harness or "")
    if modes is None:
        return None
    if not args:
        return "default" if "default" in modes else None
    bypass = _BYPASS_LAUNCH_ARGS.get(harness or "")
    if bypass is not None and tuple(args) == bypass[0]:
        return bypass[1]
    return next((name for name, mode_args in modes.items() if mode_args == args), None)


def launch_args_for_mode(harness: str | None, mode: str) -> list[str] | None:
    """Launch args of *mode*, for a mode the agent's default has already allowed."""
    bypass = _BYPASS_LAUNCH_ARGS.get(harness or "")
    if bypass is not None and mode == bypass[1]:
        return list(bypass[0])
    return list(PERMISSION_LAUNCH_ARGS.get(harness or "", {}).get(mode, [])) or None


def permission_mode_is_looser(harness: str | None, mode: str | None, default: str | None) -> bool:
    """Whether *mode* ranks looser than *default* for *harness*; unknown modes never do."""
    order = PERMISSION_MODE_STRICTNESS.get(harness or "", ())
    return mode in order and default in order and order.index(mode) > order.index(default)


def permission_mode_exceeds_default(
    harness: str | None, mode: str | None, default: str | None
) -> bool:
    """Whether *mode* is looser than *default*, or skips approval when no default is declared."""
    if mode is None:
        return False
    if default is None:
        return mode in APPROVAL_SKIPPING_PERMISSION_MODES
    return permission_mode_is_looser(harness, mode, default)


def check_permission_mode_not_looser(harness: str | None, mode: str, default: str | None) -> None:
    """
    Refuse *mode* when it is looser than the agent's *default*.

    The agent's definition is the owner's word on how far it may skip approval; a mode a
    session asks for is picked by an agent, and what it reads can steer that pick.

    :param harness: Canonical harness of the new session.
    :param mode: The mode the request names.
    :param default: The agent's default mode, or ``None`` when its definition declares no
        stance, which allows any mode that asks for approval.
    :raises ValueError: *mode* is looser than the default, or not a mode of the harness.
    """
    order = PERMISSION_MODE_STRICTNESS.get(harness or "", ())
    if mode not in order:
        choices = [name for name in order if name not in APPROVAL_SKIPPING_PERMISSION_MODES]
        supported = f"use one of {', '.join(map(repr, choices))}" if choices else "omit it"
        raise ValueError(
            f"permission_mode {mode!r} is not a mode of {harness or 'this agent'!r}; {supported}"
        )
    if default is None:
        if mode in APPROVAL_SKIPPING_PERMISSION_MODES:
            raise ValueError(
                f"permission_mode {mode!r} skips approval, but this agent's definition "
                "declares no default that skips it; omit permission_mode or choose a mode "
                "that asks for approval"
            )
        return
    if permission_mode_is_looser(harness, mode, default):
        raise ValueError(
            f"permission_mode {mode!r} is looser than this agent's default {default!r}; "
            "a request can only tighten the default, so omit it or choose a stricter mode"
        )
