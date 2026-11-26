"""A lead's wait, read from its ``Waits:`` trailer exactly and from its prose as the fallback."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "omnigent_agent_waits",
    Path(__file__).resolve().parent.parent
    / "skills"
    / "omnigent-agents"
    / "scripts"
    / "omnigent_agent.py",
)
oa = importlib.util.module_from_spec(spec)
sys.modules["omnigent_agent_waits"] = oa
spec.loader.exec_module(oa)

CHILD = "7bcb64cde5d748fcbb2e9edf7bc77e5a"
COS_TEXT = "STILL WORKING: waiting on the CoS spec approval and merge of axis#976"
CLOUD_TEXT = f"STILL WORKING: waiting on cloud session {CHILD} (check:oracle profile)"


def waits_on(text: str, notify: str = "cos-session") -> bool:
    lead = {"named": oa.named_of(text)}
    return oa.waits_on(text, lead, notify)


def test_a_wait_on_the_cos_is_a_wait_on_the_supervisor() -> None:
    assert waits_on(COS_TEXT)
    assert waits_on("STILL WORKING: waiting on the chief of staff to approve")
    assert waits_on("STILL WORKING: spec filed.\nCoS: please approve #12")
    assert not waits_on("STILL WORKING: waiting on CI for #12")
    assert not waits_on("STILL WORKING: waiting on a cosmetic review")


def test_a_session_id_after_more_than_one_word_is_a_named_wait() -> None:
    assert oa.wait_of(CLOUD_TEXT) == CHILD
    assert oa.named_of(CLOUD_TEXT) == [CHILD]


def test_an_id_on_a_later_line_of_the_wait_is_named_but_not_one_after_a_blank_line() -> None:
    assert oa.named_of(f"STILL WORKING: gate\nrunning in {CHILD}") == [CHILD]
    assert oa.named_of(f"STILL WORKING: gate\n\nan old note: {CHILD}") == []


@pytest.mark.parametrize(
    ("trailer", "wait"),
    [
        ("Waits: supervisor", "supervisor"),
        (f"Waits: session {CHILD}", CHILD),
        ("Waits: #976", "#976"),
        ("Waits: owner/repo#976", "owner/repo#976"),
        ("Waits: CI", "CI"),
        ("Waits: until 2026-10-21T23:17Z", "until 2026-10-21T23:17Z"),
        ("**Waits:** until 2026-10-21 23:17 UTC", "until 2026-10-21T23:17Z"),
    ],
)
def test_a_waits_trailer_is_read_exactly(trailer: str, wait: str) -> None:
    text = f"STILL WORKING: waiting on the verifier for lunch\n{trailer}"
    assert oa.wait_of(text) == wait


def test_a_supervisor_trailer_routes_to_the_supervisor_and_any_other_one_does_not() -> None:
    assert waits_on("STILL WORKING: spec filed\nWaits: supervisor")
    assert waits_on(f"STILL WORKING: x\nWaits: session {'c' * 32}", notify="c" * 32)
    # exact: a trailer naming CI wins over prose that mentions the CoS
    assert not waits_on("STILL WORKING: waiting on the CoS to look later\nWaits: CI")


def test_a_trailer_session_is_named_and_a_malformed_one_falls_back_to_prose() -> None:
    assert oa.named_of(f"STILL WORKING: x\nWaits: session {CHILD}") == [CHILD]
    assert oa.wait_of("STILL WORKING: waiting on the verifier\nWaits: soon") == "verifier"
    assert oa.wait_of("Waits: CI") == "unknown"  # no STILL WORKING: no wait


def test_wait_on_prints_only_a_well_formed_line_the_parser_reads_back() -> None:
    for words, wait in [
        (["supervisor"], "supervisor"),
        (["session", CHILD], CHILD),
        (["pr", "976"], "#976"),
        (["ci"], "CI"),
        (["until", "2026-10-21T23:17Z"], "until 2026-10-21T23:17Z"),
    ]:
        line = oa.wait_line(words)
        assert oa.wait_of(f"STILL WORKING: x\n{line}") == wait
    for bad in (["session", "ef38cdf6"], ["pr"], ["ci", "now"], ["until", "2026-13-40T99:99Z"], []):
        with pytest.raises(oa.Fail):
            oa.wait_line(bad)


@pytest.mark.parametrize(
    ("text", "child"),
    [
        (
            (
                "STILL WORKING: waiting on cloud worker session db6eead2cb8646eba42e47039543b187"
                " (build of #985 on PR #986)."
            ),
            "db6eead2cb8646eba42e47039543b187",
        ),
        (
            (
                "STILL WORKING: waiting on verifier pass 2 (session 42272f5175ea4d6d8191e68730720884)"
                " and CI for #1291."
            ),
            "42272f5175ea4d6d8191e68730720884",
        ),
    ],
)
def test_a_child_id_inside_the_sentence_is_the_wait(text: str, child: str) -> None:
    assert oa.wait_of(text) == child
    assert oa.named_of(text) == [child]


def test_a_waits_line_may_name_a_set() -> None:
    text = "STILL WORKING: x\nWaits: #12, o/r#13 and session " + "a" * 32
    assert oa.wait_of(text) == f"#12, o/r#13, {'a' * 32}"
    assert oa.wait_of("STILL WORKING: x\nWaits: #12, soon") != "#12, soon"  # all or none
    assert oa.wait_of("STILL WORKING: x\nWaits: supervisor, #12") == "supervisor"


def test_a_mistyped_time_in_a_set_voids_the_line() -> None:
    assert oa.trailer_items("STILL WORKING: x\nWaits: until 2026-13-40T99:99Z, #12") == []
