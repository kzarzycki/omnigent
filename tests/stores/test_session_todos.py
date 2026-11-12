import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from omnigent.native.session_todos import validate_session_todos
from omnigent.stores.conversation_store import MAX_OPEN_QUESTIONS, OpenQuestionLimitError
from omnigent.stores.conversation_store import sqlalchemy_store as store_module

TODOS = [{"content": "Verify the example", "status": "in_progress", "activeForm": "Verifying"}]


def test_plan_validation_is_canonical_and_bounded():
    assert validate_session_todos([{**TODOS[0], "unknown": "discard"}]) == TODOS
    assert validate_session_todos([{**TODOS[0], "status": []}]) == []
    invalid = (
        TODOS * 101,
        [{**TODOS[0], "content": "x" * 4097}],
        [{**TODOS[0], "content": "字" * 4096, "activeForm": "字" * 4096}] * 11,
    )
    for value in invalid:
        with pytest.raises(ValueError):
            validate_session_todos(value)


def test_sqlite_writers_preserve_policy_and_plan(tmp_path, monkeypatch):
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'concurrent.db'}")
    conv = store.create_conversation(title="Concurrent Plan fixture")
    entered = [threading.Event(), threading.Event()]
    release, decode = threading.Event(), store_module._decode_session_state

    def coordinated_decode(value: str | None) -> tuple[dict[str, object], dict[str, object]]:
        state, reserved = decode(value)
        writer = int(threading.current_thread().name.rsplit("_", 1)[1])
        entered[writer].set()
        assert writer != 0 or release.wait(10)
        return state, reserved

    monkeypatch.setattr(store_module, "_decode_session_state", coordinated_decode)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="writer") as executor:
        policy_write = executor.submit(store.set_session_state, conv.id, {"policy_counter": 1})
        assert entered[0].wait(10)
        plan_write = executor.submit(store.set_session_todos, conv.id, TODOS)
        blocked = not entered[1].wait(0.5)
        release.set()
        assert blocked
        assert policy_write.result(timeout=10) is None and plan_write.result(timeout=10)

    monkeypatch.setattr(store_module, "_decode_session_state", decode)
    reloaded = store.get_conversation(conv.id)
    assert reloaded and reloaded.session_state == {"policy_counter": 1}
    assert reloaded.session_todos == TODOS


def test_legacy_session_state_rows_read_and_upgrade_cleanly(tmp_path):
    """Rows persisted before Plan storage existed keep their policy state."""
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'legacy.db'}")
    conv = store.create_conversation(title="Legacy fixture")
    # A pre-upgrade server wrote the policy dict alone, with json.dumps defaults.
    legacy_state = {"policy_counter": 7, "flags": {"beta": True}}
    with store._session("test_seed_legacy_session_state") as session:
        session.execute(
            update(store_module.SqlConversationMetadata)
            .where(store_module.SqlConversationMetadata.id == conv.id)
            .values(session_state=json.dumps(legacy_state))
        )

    loaded = store.get_conversation(conv.id)
    assert loaded and loaded.session_state == legacy_state
    assert loaded.session_todos == []

    # The first new-format Plan write must not disturb legacy policy keys.
    assert store.set_session_todos(conv.id, TODOS)
    upgraded = store.get_conversation(conv.id)
    assert upgraded and upgraded.session_state == legacy_state
    assert upgraded.session_todos == TODOS

    # Clearing the Plan leaves the row policy-only again, as before the upgrade.
    assert store.set_session_todos(conv.id, [])
    cleared = store.get_conversation(conv.id)
    assert cleared and cleared.session_state == legacy_state
    assert cleared.session_todos == []


def test_open_questions_outlive_policy_and_plan_writes(tmp_path):
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'questions.db'}")
    conv = store.create_conversation(title="Open question fixture")
    card = {"type": "response.elicitation_request", "elicitation_id": "question_a"}

    assert store.add_open_question(conv.id, "question_a", card)
    store.set_session_state(conv.id, {"policy_counter": 2})
    assert store.set_session_todos(conv.id, TODOS)

    loaded = store.get_conversation(conv.id)
    assert loaded and loaded.open_questions == {"question_a": card}
    assert loaded.session_state == {"policy_counter": 2}
    assert loaded.session_todos == TODOS

    assert store.remove_open_question(conv.id, "question_a")
    assert store.remove_open_question(conv.id, "question_unknown")
    cleared = store.get_conversation(conv.id)
    assert cleared and cleared.open_questions == {}
    assert cleared.session_state == {"policy_counter": 2}
    assert not store.add_open_question("0" * 32, "question_b", card)


def test_open_questions_are_bounded_per_session(tmp_path):
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'bounded.db'}")
    conv = store.create_conversation(title="Bounded question fixture")
    # Ancestor copies of sub-agents' cards are not the session's own questions.
    for index in range(2):
        copy = {"elicitation_id": f"question_child_{index}", "params": {"target_session_id": "c"}}
        assert store.add_open_question(conv.id, f"question_child_{index}", copy)
    for index in range(MAX_OPEN_QUESTIONS):
        store.add_open_question(
            conv.id, f"question_{index}", {"elicitation_id": f"question_{index}"}
        )

    with pytest.raises(OpenQuestionLimitError):
        store.add_open_question(conv.id, "question_extra", {"elicitation_id": "question_extra"})
    copy = {"elicitation_id": "question_child_2", "params": {"target_session_id": "c"}}
    assert store.add_open_question(conv.id, "question_child_2", copy)
    # Re-storing a card already held does not count against the limit.
    assert store.add_open_question(conv.id, "question_0", {"elicitation_id": "question_0"})
