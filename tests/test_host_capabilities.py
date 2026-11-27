"""Capability tags of hosts and cloud kinds."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame
from omnigent.host_capabilities import (
    detected_os_tag,
    is_capability_tag,
    load_host_capability_tags,
    missing_needs,
    normalize_capability_tags,
)


def test_tag_shape() -> None:
    assert is_capability_tag("tool:xcode")
    assert is_capability_tag("device:iphone-15")
    assert not is_capability_tag("Tool:Xcode")
    assert not is_capability_tag("xcode")
    assert not is_capability_tag("tool:")
    assert not is_capability_tag(3)


def test_normalize_drops_malformed_and_dedupes() -> None:
    assert normalize_capability_tags(["b:x", "a:y", "b:x", "bad", 1]) == ["a:y", "b:x"]
    assert normalize_capability_tags("os:linux") == []


def test_missing_needs_is_a_subset_check() -> None:
    assert missing_needs(["os:macos"], ["os:macos", "tool:xcode"]) == []
    assert missing_needs(["tool:xcode", "os:macos"], ["os:linux"]) == ["os:macos", "tool:xcode"]
    assert missing_needs([], None) == []
    assert missing_needs(["os:macos"], None) == ["os:macos"]


def test_host_config_tags_get_the_os_tag(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("host:\n  capabilities: [tool:xcode, device:iphone-15]\n")

    assert load_host_capability_tags(cfg) == sorted(
        ["tool:xcode", "device:iphone-15", detected_os_tag()]
    )


def test_missing_config_gives_only_the_os_tag(tmp_path: Path) -> None:
    assert load_host_capability_tags(tmp_path / "none.yaml") == [detected_os_tag()]


@pytest.mark.parametrize("value", ["[Xcode]", "tool:xcode", "[3]"])
def test_malformed_config_is_refused(tmp_path: Path, value: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"host:\n  capabilities: {value}\n")

    with pytest.raises(ValueError, match=r"host\.capabilities"):
        load_host_capability_tags(cfg)


def test_hello_frame_round_trips_capabilities() -> None:
    hello = HostHelloFrame(
        version="0", frame_protocol_version=1, name="h", host_capabilities=["os:linux"]
    )

    decoded = decode_host_frame(encode_host_frame(hello))

    assert isinstance(decoded, HostHelloFrame)
    assert decoded.host_capabilities == ["os:linux"]
    old = decode_host_frame(
        encode_host_frame(HostHelloFrame(version="0", frame_protocol_version=1, name="h"))
    )
    assert isinstance(old, HostHelloFrame)
    assert old.host_capabilities is None
