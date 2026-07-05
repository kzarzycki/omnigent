# Superpowers Source

This directory vendors the Codex-compatible Superpowers skills from:

- Repository: https://github.com/obra/superpowers
- Upstream commit: d884ae04edebef577e82ff7c4e143debd0bbec99
- Upstream plugin version: 6.1.1

The skills are installed under `examples/polly/agents/codex/skills/` because
Omnigent's Codex-native launcher populates each private per-session
`CODEX_HOME/skills/` from the agent bundle's `skills/` directory. Keeping the
payload in the agent bundle makes the install repo-local and reproducible for
Codex sessions launched from this checkout.
