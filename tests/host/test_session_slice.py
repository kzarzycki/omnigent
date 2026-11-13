"""Tests for the host's memory-limited session slice."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from omnigent.host import session_slice
from omnigent.host.session_slice import (
    SessionSliceConfig,
    activate_session_slice,
    load_session_slice_config,
)
from omnigent.util import user_systemd


def _write_config(tmp_path: Path, host_section: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"host": host_section}))
    return path


@pytest.mark.parametrize(
    "host_section",
    [{}, {"session_slice": {"enabled": False, "memory_max": "8G"}}, {"session_slice": {}}],
    ids=["missing", "off", "enabled-missing"],
)
def test_load_session_slice_config_is_off_by_default(
    tmp_path: Path, host_section: dict[str, object]
) -> None:
    """A missing section, a missing flag and ``enabled: false`` all read as off."""
    assert load_session_slice_config(_write_config(tmp_path, host_section)) is None
    assert load_session_slice_config(tmp_path / "absent.yaml") is None


def test_load_session_slice_config_reads_name_and_limits(tmp_path: Path) -> None:
    """The limits become systemd property assignments in a fixed order."""
    path = _write_config(
        tmp_path,
        {
            "session_slice": {
                "enabled": True,
                "name": "agents.slice",
                "memory_swap_max": "1G",
                "memory_max": 9663676416,
                "memory_high": "80%",
            }
        },
    )
    assert load_session_slice_config(path) == SessionSliceConfig(
        name="agents.slice",
        properties=("MemoryHigh=80%", "MemoryMax=9663676416", "MemorySwapMax=1G"),
    )


def test_load_session_slice_config_defaults_the_slice_name(tmp_path: Path) -> None:
    """``enabled: true`` alone uses the default slice and sets no limit."""
    path = _write_config(tmp_path, {"session_slice": {"enabled": True}})
    assert load_session_slice_config(path) == SessionSliceConfig(
        name="omnigent-sessions.slice", properties=()
    )


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("on", r"host\.session_slice in"),
        ({"enabled": "yes"}, r"host\.session_slice\.enabled"),
        ({"enabled": True, "name": "sessions.scope"}, r"host\.session_slice\.name"),
        ({"enabled": True, "memory_max": "8 gigs"}, r"host\.session_slice\.memory_max"),
        ({"enabled": True, "memory_high": True}, r"host\.session_slice\.memory_high"),
    ],
)
def test_load_session_slice_config_refuses_bad_values(
    tmp_path: Path, section: object, key: str
) -> None:
    """A malformed value is refused with a message naming its key."""
    path = _write_config(tmp_path, {"session_slice": section})
    with pytest.raises(ValueError, match=key):
        load_session_slice_config(path)


def test_activate_session_slice_off_launches_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the setting off nothing runs and no launch prefix is returned."""
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    assert activate_session_slice(None) is None


def test_activate_session_slice_without_systemd_logs_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A host without systemd (e.g. macOS) warns once and launches as before."""
    monkeypatch.setattr(session_slice, "IS_LINUX", False)
    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    config = SessionSliceConfig(name="omnigent-sessions.slice", properties=("MemoryMax=8G",))
    assert activate_session_slice(config) is None
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert "omnigent-sessions.slice" in caplog.text


def test_activate_session_slice_without_memory_delegation_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A user manager that does not delegate ``memory`` cannot carry the limits."""
    _pretend_systemd_installed(monkeypatch)
    calls = _record_systemctl(monkeypatch, subtree_control="cpu pids", cgroup_root=tmp_path)
    config = SessionSliceConfig(name="omnigent-sessions.slice", properties=("MemoryMax=8G",))
    assert activate_session_slice(config) is None
    assert "memory controller" in caplog.text
    assert not any("set-property" in call for call in calls)


def test_activate_session_slice_on_sets_limits_and_returns_scope_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the setting on the limits are applied and launches go through a scope."""
    _pretend_systemd_installed(monkeypatch)
    calls = _record_systemctl(monkeypatch, subtree_control="cpu memory pids", cgroup_root=tmp_path)
    config = SessionSliceConfig(
        name="omnigent-sessions.slice", properties=("MemoryHigh=8G", "MemoryMax=9G")
    )
    launch = activate_session_slice(config)
    assert launch is not None
    assert launch.name == "omnigent-sessions.slice"
    prefix = launch.command_prefix
    assert calls[-1] == [
        "systemctl",
        "--user",
        "set-property",
        "--runtime",
        "omnigent-sessions.slice",
        "MemoryHigh=8G",
        "MemoryMax=9G",
    ]
    assert prefix[:5] == ("systemd-run", "--user", "--scope", "--quiet", "--collect")
    assert "--slice=omnigent-sessions.slice" in prefix
    assert prefix[-1] == "--"


def test_activate_session_slice_survives_a_failed_set_property(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A user manager that rejects the limits leaves launches as before."""
    _pretend_systemd_installed(monkeypatch)
    _record_systemctl(
        monkeypatch,
        subtree_control="cpu memory pids",
        cgroup_root=tmp_path,
        set_property_error="Unknown assignment",
    )
    config = SessionSliceConfig(name="omnigent-sessions.slice", properties=("MemoryMax=9G",))
    assert activate_session_slice(config) is None
    assert "Unknown assignment" in caplog.text


def _pretend_systemd_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(user_systemd, "_scope_oom_policy_supported", lambda: True)
    for module in (session_slice, user_systemd):
        monkeypatch.setattr(module, "IS_LINUX", True)
        monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")


def _fail_if_called(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"unexpected subprocess.run{args}")


def _record_systemctl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subtree_control: str,
    cgroup_root: Path,
    set_property_error: str | None = None,
) -> list[list[str]]:
    """Fake ``systemctl --user`` against a cgroup tree under *cgroup_root*."""
    manager_cgroup = cgroup_root / "user.slice" / "user@1000.service"
    manager_cgroup.mkdir(parents=True)
    (manager_cgroup / "cgroup.subtree_control").write_text(subtree_control + "\n")
    monkeypatch.setattr(user_systemd, "_CGROUP_ROOT", cgroup_root)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "show" in argv:
            return subprocess.CompletedProcess(argv, 0, "/user.slice/user@1000.service\n", "")
        if set_property_error is not None:
            raise subprocess.CalledProcessError(1, argv, "", set_property_error)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


_PREFIX = ("systemd-run", "--user", "--scope", "--slice=omnigent-sessions.slice", "--")


class _RecordedPopen:
    """Stands in for ``subprocess.Popen``, recording the argv it was given."""

    calls: list[list[str]] = []

    def __init__(self, argv: list[str], **kwargs: object) -> None:
        _RecordedPopen.calls.append(list(argv))
        self.pid = 4242


def test_direct_runner_launch_goes_through_the_slice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the zygote off, the direct Popen runner starts in the slice too."""
    from omnigent.host import connect
    from omnigent.host.identity import HostIdentity
    from omnigent.host.session_slice import SessionSliceLaunch

    host = connect.HostProcess(
        HostIdentity(host_id="host_session_slice", name="slice-test"),
        "http://localhost:8000",
        session_slice=SessionSliceLaunch(name="omnigent-sessions.slice", command_prefix=_PREFIX),
    )
    host._zygote_disabled = True
    _RecordedPopen.calls = []
    monkeypatch.setattr(connect.subprocess, "Popen", _RecordedPopen)
    env: dict[str, str] = {"RUNNER_ID": "runner_slice"}
    host._spawn_runner_proc(env, "slug", tmp_path)
    (argv,) = _RecordedPopen.calls
    assert tuple(argv[: len(_PREFIX)]) == _PREFIX
    assert argv[len(_PREFIX) :][1:] == ["-P", "-m", "omnigent.runner._entry"]
    assert env["OMNIGENT_SESSION_SLICE"] == "omnigent-sessions.slice"


def test_zygote_launch_goes_through_the_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runners the zygote forks inherit its scope, so the zygote starts in the slice."""
    from omnigent.host import runner_zygote

    _RecordedPopen.calls = []
    monkeypatch.setattr(runner_zygote.subprocess, "Popen", _RecordedPopen)
    runner_zygote.ZygoteManager(command_prefix=_PREFIX)._spawn_zygote_process(0)
    (argv,) = _RecordedPopen.calls
    assert tuple(argv[: len(_PREFIX)]) == _PREFIX
    assert argv[-3:] == ["-P", "-m", "omnigent.runner._zygote"]
