import sys

import pytest


@pytest.fixture(autouse=True)
def no_origin_reads(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No supervisor test reads the checkout's origin, since a real read can hang to the 60 s timeout on a slow
    network and makes a test's result depend on the checkout it runs in (#2384):
    - `read_loop_events` is a real `ls-remote` and fetch, which also moves the tests' clocks (#1377): it reads none.
      A module that reads a real bare remote sets `READS_LOOP_EVENTS = True`.
    - `lead_delegation` fetches origin's main for its base policy, with no timeout: it gives the plain wording, `""`.
      A module that tests the reader itself sets `READS_LEAD_DELEGATION = True`.
    It lives here, not in test_omnigent_agent.py, because an autouse fixture applies only to the module that defines
    or imports it."""
    agent = sys.modules.get("omnigent_agent")
    if agent is None:
        return
    if not getattr(request.module, "READS_LOOP_EVENTS", False):
        monkeypatch.setattr(agent, "read_loop_events", lambda cwd=None: None)
    if not getattr(request.module, "READS_LEAD_DELEGATION", False):
        monkeypatch.setattr(agent, "lead_delegation", lambda *_, **__: "")
