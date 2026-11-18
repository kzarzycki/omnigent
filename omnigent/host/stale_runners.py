"""Host-side checks for replacing adopted runners that run an older build."""

from __future__ import annotations

from pathlib import Path

from omnigent.native import owner_claim


def runner_has_attached_terminal(pid: int, *, tmp_root: Path | None = None) -> bool:
    """Whether a terminal client (not the web bridge) is attached to a tmux pane *pid* owns.

    Each runner terminal lives in a private temp dir whose owner claim names the
    runner's pid, so the host finds them on disk without asking the runner.

    :param pid: The runner's pid, e.g. ``4242``.
    :param tmp_root: Directory holding terminal instance dirs; the runner's
        temp dir when ``None``.
    :returns: ``True`` when a regular tmux client is attached to one of them.
    """
    from omnigent.inner.terminal import _TERMINAL_DIR_PREFIX, _terminals_tmp_root
    from omnigent.native.native_cost_popup import _tmux_last_client_input_at

    root = tmp_root if tmp_root is not None else _terminals_tmp_root()
    pid_ns = owner_claim.current_pid_namespace()
    for entry in root.glob(f"{_TERMINAL_DIR_PREFIX}*"):
        claim = owner_claim.read_owner_claim(entry)
        if claim is None or claim.pid != pid or claim.pid_ns != pid_ns:
            continue
        socket_path = entry / "tmux.sock"
        if (
            socket_path.exists()
            and _tmux_last_client_input_at(str(socket_path), "main") is not None
        ):
            return True
    return False
