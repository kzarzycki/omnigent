#!/usr/bin/env bash
# Ancestor resolves a descendant's question card (issue 490): server flow, then the web card and event.
set -euo pipefail
uv run --no-sync pytest -q tests/server/integration/test_sys_ask_user_question.py
(cd web && npx vitest run src/components/blocks/ApprovalCard.test.tsx src/lib/sse.test.ts)
