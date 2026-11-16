"""The sys_session_create schema must say a named environment is top-level."""

from omnigent.tools.builtins.spawn import SysSessionCreateTool

ENVIRONMENT_SENTENCE = (
    "With placement='same_host', an environment_id starts the session on the host "
    "and workspace its project pins, top-level in that project, outside this "
    "session's family."
)


def test_description_states_named_environment_is_top_level() -> None:
    assert ENVIRONMENT_SENTENCE in SysSessionCreateTool.description()


def test_environment_and_placement_properties_state_top_level() -> None:
    properties = SysSessionCreateTool().get_schema()["function"]["parameters"]["properties"]
    assert "top-level session" in properties["environment_id"]["description"]
    assert "top-level" in properties["placement"]["description"]
