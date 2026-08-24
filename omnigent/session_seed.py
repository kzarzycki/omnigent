"""Optional title + project label for a newly created CLI session.

Read from the environment so a headless caller (e.g. the audited auto-sync job)
can file its recurring sessions into one collapsible sidebar folder instead of
cluttering the top-level list. Both variables are unset by default, so an
ordinary interactive run is unaffected.

Shared by every CLI session-create path — the interactive REPL
(``omnigent/repl/_repl.py``) and the headless ``-p`` prompt
(``omnigent/chat.py``) — so grouping doesn't depend on which one ran.
"""

from __future__ import annotations

import os

# Reserved sidebar-grouping label (conversation store's ``PROJECT_LABEL_KEY``);
# a session carrying it shows under that project folder in the web UI.
PROJECT_LABEL_KEY = "omni_project"


def session_seed_from_env() -> tuple[str | None, dict[str, str] | None]:
    """Title and labels to seed a new session with, from the environment.

    ``OMNIGENT_SESSION_TITLE`` sets the session title; ``OMNIGENT_SESSION_PROJECT``
    files it into a sidebar project via the ``omni_project`` label.

    :returns: ``(title_or_None, labels_or_None)``.
    """
    title = os.environ.get("OMNIGENT_SESSION_TITLE") or None
    project = os.environ.get("OMNIGENT_SESSION_PROJECT") or None
    labels = {PROJECT_LABEL_KEY: project} if project else None
    return title, labels
