"""Owner claims for tests that need a chosen pid as a native dir's owner."""

from __future__ import annotations

from pathlib import Path

from omnigent.native import owner_claim


def claim_owner(marker: Path, pid: int, *, pid_ns: str | None = None) -> None:
    """Write a claim for *pid* at *marker*, in this PID namespace and boot unless *pid_ns*."""
    owner_claim.write_owner_claim(marker.parent, filename=marker.name)
    lines = marker.read_text(encoding="utf-8").splitlines()
    lines[0] = str(pid)
    if pid_ns is not None:
        lines[1] = f"pid_ns={pid_ns}"
    marker.write_text("\n".join(lines) + "\n", encoding="utf-8")
