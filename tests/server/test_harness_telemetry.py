"""The server's harness telemetry setting and the launch decision it makes per session."""

from __future__ import annotations

import pytest

from omnigent.host.frames import CAP_HARNESS_TELEMETRY
from omnigent.server.harness_telemetry import harness_telemetry_launch, harness_telemetry_setting
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_PROJECT = "0123456789abcdef0123456789abcd02"
_OTHER_PROJECT = "0123456789abcdef0123456789abcd03"
_CAPS = [CAP_HARNESS_TELEMETRY]


class _Owners:
    """Conversation-store stand-in answering only the owner lookup."""

    def __init__(self, owners: dict[str, str]) -> None:
        self.owners = owners

    def get_session_owner(self, conversation_id: str) -> str | None:
        return self.owners.get(conversation_id)


@pytest.fixture
def server_config(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Write the server config YAML the launch decision reads, and its headers env."""
    path = tmp_path / "server.yaml"
    monkeypatch.setenv("OMNIGENT_CONFIG", str(path))
    monkeypatch.setenv("OMNIGENT_HARNESS_TELEMETRY_HEADERS", "x-ingest-token=made-up%2Ctoken")

    def write(text: str) -> None:
        path.write_text(text)

    return write


_SETTING = f"""
harness_telemetry:
  endpoint: https://otlp.example.test/
  exclude:
    users: [excluded@example.test]
    projects: [{_PROJECT}]
    threads: [THREAD]
    repos: [git.example.test/team/private]
    labels: ["omni.role=verifier"]
"""


def test_launch_decision_for_each_exclusion_kind(db_uri: str, server_config) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    thread = store.create_conversation()
    server_config(_SETTING.replace("THREAD", thread.id))
    by_user = store.create_conversation()
    by_project = store.create_conversation(project_id=_PROJECT)
    by_label = store.create_conversation()
    store.set_labels(by_label.id, {"omni.role": "verifier"})
    by_label = store.get_conversation(by_label.id)
    sub_session = store.create_conversation(parent_conversation_id=thread.id)
    owners = _Owners({by_user.id: "excluded@example.test"})

    for conv in (thread, by_user, by_project, by_label, sub_session):
        assert conv is not None
        assert harness_telemetry_launch(conv, _CAPS, owners) == {"enabled": False}, conv.id


def test_launch_decision_for_an_allowed_session_carries_endpoint_headers_and_attributes(
    db_uri: str, server_config
) -> None:
    server_config(_SETTING.replace("THREAD", "conv_other"))
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(project_id=_OTHER_PROJECT)
    child = store.create_conversation(parent_conversation_id=root.id, project_id=_OTHER_PROJECT)
    # A sub-session without its own grant takes its root's owner, who is allowed.
    owners = _Owners({root.id: "member@example.test"})

    decision = harness_telemetry_launch(child, _CAPS, owners)

    assert decision == {
        "enabled": True,
        "endpoint": "https://otlp.example.test",
        "headers": {"x-ingest-token": "made-up,token"},
        "exclude_repos": ["git.example.test/team/private"],
        "resource_attributes": {
            "omnigent.session.id": child.id,
            "omnigent.root_session.id": root.id,
            "omnigent.project": _OTHER_PROJECT,
        },
    }


def test_a_sub_session_of_an_excluded_users_thread_is_excluded(db_uri: str, server_config) -> None:
    server_config(_SETTING)
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=root.id)

    owners = _Owners({root.id: "excluded@example.test"})

    assert harness_telemetry_launch(child, _CAPS, owners) == {"enabled": False}


def test_no_setting_or_a_host_without_the_capability_gets_no_field(
    db_uri: str, server_config
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    owners = _Owners({})
    server_config("session_recaps: true\n")
    assert harness_telemetry_launch(conv, _CAPS, owners) is None
    server_config(_SETTING)
    assert harness_telemetry_launch(conv, [], owners) is None


def test_a_setting_without_an_endpoint_is_ignored() -> None:
    assert harness_telemetry_setting({"harness_telemetry": {"exclude": {}}}) is None
    assert harness_telemetry_setting({"harness_telemetry": "https://otlp.example.test"}) is None
