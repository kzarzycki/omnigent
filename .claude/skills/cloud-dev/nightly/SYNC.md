# Vendored from the engineering pack

`nightly.py` and `folding.py` are unmodified copies of
`engineering/skills/engineering-loop/scripts/` in agent-skills, tag
`engineering-v0.33.2` (commit `36c9043a`). `nightly.py` imports `folding`, so both ship.

To update: copy both files from the newer pack release, then edit the tag above.
The workflow `fork-nightly.yml` calls `nightly.py red` when the nightly fails.
