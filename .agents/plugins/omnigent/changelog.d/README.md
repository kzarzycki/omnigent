# Unreleased changelog entries

Each change adds its entry here as its own file, `<issue>.md`, holding the bullet it would
have added under `## Unreleased` in [../CHANGELOG.md](../CHANGELOG.md); a later change to
the same issue extends that file. Separate files keep two PRs' entries from conflicting
when either merges `main` in, while two edits to the same entry still conflict (#244).

A release moves every entry here, in issue order, under its version heading in
`CHANGELOG.md`, after any still under `## Unreleased`, and deletes the files. One run of the
Release workflow (`gh workflow run release.yml`) does that for every package with entries, in
one PR, and tags each package on the merge commit.

How the release runs, its pre-tag smoke run and `release-check` included, is in [../../engineering/RELEASING.md](../../engineering/RELEASING.md).
