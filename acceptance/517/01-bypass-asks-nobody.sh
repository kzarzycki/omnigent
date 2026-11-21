#!/bin/sh
# A bypass session raises no approval card; other modes and questions keep theirs (issue 517).
exec uv run --no-sync pytest -q -p no:cacheprovider \
  tests/server/integration/test_sessions_permission_request_hook.py \
  tests/server/integration/test_sessions_elicitation_resolve_url.py \
  -k "bypass or never_ask or asking_session or claim or shows_question_with_options or denies_unshowable_question or keeps_question_permission_wording or ineligible_auto_mode or mirror or parent_reaches or forged or checks_ownership_for_the_authenticated"
