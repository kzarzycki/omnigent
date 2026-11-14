"""The sys_session_create schema must say a named project is top-level."""

from omnigent.tools.builtins.spawn import SysSessionCreateTool

TOP_LEVEL_SENTENCE = (
    "Naming project_id or environment_id creates a top-level session in that "
    "project, outside this session's family."
)


def test_description_states_named_project_is_top_level() -> None:
    assert TOP_LEVEL_SENTENCE in SysSessionCreateTool.description()


def test_project_and_environment_properties_state_top_level() -> None:
    properties = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]
    for field in ("project_id", "environment_id"):
        assert "top-level session" in properties[field]["description"]
    assert "top-level" in properties["placement"]["description"]
