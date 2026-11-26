"""approvals.py: a cloud land accepts a signing key a one-line environment variable holds (#1105)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from approvals_harness import (
    approvals,
    lands,  # noqa: F401
)
from test_approvals import DROP_RECORDS, PUSH_RECORD, RECORD_REF, cloud_land, unmoved  # noqa: F401

SHAPES = {
    "one line, no armour": lambda body: "".join(body),
    "no armour, CRLF, spaces and tabs": lambda body: "\r\n ".join(body) + "\t",
    "literal backslash-n": lambda body: (
        "-----BEGIN OPENSSH PRIVATE KEY-----\\n"
        + "\\n".join(body)
        + "\\n-----END OPENSSH PRIVATE KEY-----\\n"
    ),
}


@pytest.mark.parametrize("shape", SHAPES)
def test_a_cloud_land_signs_with_a_key_a_one_line_variable_holds(
    cloud_land: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    body = (cloud_land["keys"] / "landing").read_text().splitlines()[1:-1]
    monkeypatch.setenv("LAND_KEY", SHAPES[shape](body))
    for name, real in (
        ("record_ref", RECORD_REF),
        ("push_record", PUSH_RECORD),
        ("drop_records", DROP_RECORDS),
    ):
        monkeypatch.setattr(approvals, name, real)
    monkeypatch.setattr(approvals, "post_status", lambda *_a, **_k: None)
    lines = cloud_land["out"](capsys, cloud_land["land"]("7"))
    tip = cloud_land["main"]()
    assert f"PR #7 landed in main as {tip}, signed by a key in signers" in lines
    signed = subprocess.run(
        ["git", *("-c", "gpg.ssh.program=ssh-keygen"), *("-c", "gpg.ssh.allowedSignersFile=signers"),
         "verify-commit", tip],
        cwd=cloud_land["work"], capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert signed.returncode == 0, signed.stderr


def test_a_broken_one_line_key_is_refused_and_leaks_no_part_of_it(
    cloud_land: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "".join((cloud_land["keys"] / "landing").read_text().splitlines()[1:-1])
    value = body[:-40]  # truncated: base64 that is no key
    monkeypatch.setenv("LAND_KEY", value)
    code, lines = cloud_land["land"]("7")
    assert (code, lines) == (
        1,
        [
            "the variable LAND_KEY (loop.md's `land.signing_key_env`) holds no SSH private key without a passphrase"
        ],
    )
    unmoved(cloud_land)
    seen = "\n".join(lines) + "".join(capsys.readouterr())
    assert not [i for i in range(len(value) - 7) if value[i : i + 8] in seen]


def test_an_armourless_value_is_rewrapped_without_its_whitespace_at_70_characters() -> None:
    # ssh-keygen skips whitespace inside the base64, so only the armour's own shape shows the strip
    body = "A" * 75 + "B" * 70
    out = approvals.armoured(" \t" + body[:30] + "\r\n " + body[30:] + "\r\n").split("\n")
    assert out == [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        body[:70],
        body[70:140],
        body[140:],
        "-----END OPENSSH PRIVATE KEY-----",
    ]
