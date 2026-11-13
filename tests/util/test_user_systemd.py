"""Tests for placing processes in the user's systemd manager."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.util import user_systemd
from omnigent.util.user_systemd import (
    UserSystemdError,
    child_slice_name,
    is_memory_limit,
    is_tasks_limit,
    move_into_new_slice,
)


@pytest.mark.parametrize("value", ["8G", "512M", "1.5G", "1073741824", "60%", "infinity"])
def test_is_memory_limit_accepts_systemd_sizes(value: str) -> None:
    assert is_memory_limit(value)


@pytest.mark.parametrize("value", ["8 G", "8GB", "-1", "", "lots", "8g"])
def test_is_memory_limit_refuses_other_text(value: str) -> None:
    assert not is_memory_limit(value)


@pytest.mark.parametrize("value", ["1", "4096", "10%", "infinity"])
def test_is_tasks_limit_accepts_counts_percentages_and_infinity(value: str) -> None:
    assert is_tasks_limit(value)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "10 %", "", "lots", "0%"])
def test_is_tasks_limit_refuses_other_text(value: str) -> None:
    assert not is_tasks_limit(value)


def test_child_slice_name_nests_one_level() -> None:
    """Dashes in the leaf are dropped so the child is not nested deeper."""
    assert child_slice_name("omnigent-sessions.slice", "conv-ab12") == (
        "omnigent-sessions-convab12.slice"
    )


def test_move_into_new_slice_creates_the_scope_and_its_slice_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limits go on the auxiliary slice in typed D-Bus form."""
    monkeypatch.setattr(user_systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: True)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(user_systemd, "_wait_until_in_unit", lambda pid, unit: None)
    move_into_new_slice(
        [4242, 4243],
        scope_name="omnigent-session-ab12.scope",
        slice_name="omnigent-sessions-ab12.slice",
        slice_properties={
            "MemoryHigh": "1.5G",
            "MemoryMax": "50%",
            "MemorySwapMax": "infinity",
            "TasksMax": "512",
            "CPUWeight": "50",
        },
        description="Omnigent session ab12",
    )
    (argv,) = calls
    # The slice name appears once as the scope's Slice= and again as the aux unit.
    slice_value = argv.index("omnigent-sessions-ab12.slice")
    aux = argv[argv.index("omnigent-sessions-ab12.slice", slice_value + 1) :]
    assert argv[8:11] == ["omnigent-session-ab12.scope", "fail", "5"]
    # An OOM kill must take the hog, not the whole scope with the runner in it.
    assert argv[argv.index("OOMPolicy") : argv.index("OOMPolicy") + 3] == [
        "OOMPolicy",
        "s",
        "continue",
    ]
    assert argv[argv.index("PIDs") : argv.index("PIDs") + 5] == ["PIDs", "au", "2", "4242", "4243"]
    assert aux == [
        "omnigent-sessions-ab12.slice",
        "7",
        "MemoryHigh",
        "t",
        str(int(1.5 * 1024**3)),
        "MemoryMaxScale",
        "u",
        str(round(0.5 * (2**32 - 1))),
        "MemorySwapMax",
        "t",
        str(2**64 - 1),
        "TasksMax",
        "t",
        "512",
        "CPUWeight",
        "t",
        "50",
        "Description",
        "s",
        "Omnigent session ab12",
        "StopWhenUnneeded",
        "b",
        "true",
    ]


def test_move_into_new_slice_reports_the_managers_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: False)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv, "", "Unit already exists\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(UserSystemdError, match="Unit already exists"):
        move_into_new_slice(
            [1],
            scope_name="a.scope",
            slice_name="a.slice",
            slice_properties={},
            description="a",
        )


@pytest.mark.parametrize(("supported", "expected"), [(True, True), (False, False)])
def test_scope_command_prefix_keeps_the_scope_alive_after_an_oom_kill(
    monkeypatch: pytest.MonkeyPatch, supported: bool, expected: bool
) -> None:
    """Older systemd rejects OOMPolicy= on scopes, so it is only sent when supported."""
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: supported)
    prefix = user_systemd.scope_command_prefix("a.slice", "runner")
    assert ("--property=OOMPolicy=continue" in prefix) is expected
    assert prefix[-1] == "--"


def test_move_into_new_slice_waits_until_the_pid_has_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The manager moves the pids in a job that can finish after the D-Bus reply."""
    monkeypatch.setattr(user_systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
    )
    readings = iter(["0::/app.slice/host.service", "0::/a.slice/a.scope"])
    reads: list[str] = []

    def fake_read_text(self: Path) -> str:
        reads.append(str(self))
        return next(readings)

    monkeypatch.setattr(user_systemd.Path, "read_text", fake_read_text)
    move_into_new_slice(
        [77], scope_name="a.scope", slice_name="a.slice", slice_properties={}, description="a"
    )
    assert reads == ["/proc/77/cgroup", "/proc/77/cgroup"]


def test_move_into_new_slice_fails_when_the_pid_never_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_systemd.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: False)
    monkeypatch.setattr(user_systemd, "_MOVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", "")
    )
    monkeypatch.setattr(user_systemd.Path, "read_text", lambda self: "0::/app.slice/x.service")
    with pytest.raises(UserSystemdError, match=r"did not move into a\.scope"):
        move_into_new_slice(
            [77], scope_name="a.scope", slice_name="a.slice", slice_properties={}, description="a"
        )


@pytest.mark.parametrize(
    ("result", "supported"),
    [
        (subprocess.CompletedProcess([], 0, "systemd 253 (253.5-1)\n+PAM +AUDIT\n", ""), True),
        (subprocess.CompletedProcess([], 0, "systemd 255 (255.4-1ubuntu8)\n", ""), True),
        (subprocess.CompletedProcess([], 0, "systemd 252 (252.22-1)\n", ""), False),
        (subprocess.CompletedProcess([], 0, "not systemd\n", ""), False),
        (OSError("systemctl missing"), False),
        (subprocess.TimeoutExpired(["systemctl"], 10), False),
    ],
    ids=["253", "255", "252", "unparsable", "missing", "timeout"],
)
def test_scope_oom_policy_supported_from_systemd_253(
    monkeypatch: pytest.MonkeyPatch, result: object, supported: bool
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> object:
        if isinstance(result, BaseException):
            raise result
        return result

    user_systemd._scope_oom_policy_supported.cache_clear()
    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        assert user_systemd._scope_oom_policy_supported() is supported
    finally:
        user_systemd._scope_oom_policy_supported.cache_clear()


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("512", ["TasksMax", "t", "512"]),
        ("infinity", ["TasksMax", "t", str(2**64 - 1)]),
        ("10%", ["TasksMaxScale", "u", str(round(0.1 * (2**32 - 1)))]),
    ],
)
def test_tasks_max_is_encoded_as_a_count_a_scale_or_infinity(
    value: str, encoded: list[str]
) -> None:
    assert user_systemd._dbus_property("TasksMax", value) == encoded
