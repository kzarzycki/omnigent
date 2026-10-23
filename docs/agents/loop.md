# Loop facts

Fork-only: upstream has no `docs/agents/loop.md`, so an upstream sync never conflicts with it. The `engineering-loop` skill reads it.

Worker model: mid for size/S or smaller

A tier, not a model id: Sonnet for Claude, resolved at launch to the newest generation. A small worker doesn't need the top tier, and the mid tier costs about half per item (agent-skills' trial). Leads and verifiers stay on the default. Undo: delete the line.
