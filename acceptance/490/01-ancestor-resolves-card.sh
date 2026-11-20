#!/usr/bin/env bash
# Ancestor resolves a descendant's question card (issue 490): server flow, then the web card and event.
set -euo pipefail
uv run --no-sync pytest -q tests/server/integration/test_sys_ask_user_question.py tests/runner/test_ask_user_question_tool.py tests/tools/test_manager.py
(cd web && npx vitest run src/components/blocks/ApprovalCard.test.tsx src/lib/sse.test.ts src/store/chatStore.test.ts)
