---
name: browsing
description: Drive and check a web page with a browser without taking the user's screen - headless first, your own browser rather than one already open, a logged-in one only when the page needs those logins - and render a page to a screenshot with browse.py. Use when checking a web UI change, rendering a page for proof, or automating a page.
metadata:
  requires: "uv"
---

# Browsing

A browser window that opens on the user's machine takes focus from what they are doing, so
take the first rung that works:

1. **A headless browser of your own**, for anything that needs none of the user's logins: a
   local server, a test, a UI check, a public page. A project's own browser e2e runs in its
   headless mode; its instructions name the switch when the default is headed.
2. **A browser holding the user's logins**, only when the page needs them, attached as the
   project's or the user's instructions say, and said in your report, since it is headed.
3. **A headed browser of your own**, only when a check cannot run headless: start it in the
   background without taking focus, and say which check needed it and why.

Never attach over CDP or a remote-debugging port to a browser already open just because it
is there: it is headed and may be someone else's session.

## Tools

- `agent-browser`, where the host has it, for interactive drives: a session per task, its
  own headless browser unless its config attaches to a logged-in one.
- `uv run <this skill's folder>/scripts/browse.py <url> --out <png>` renders a page in
  headless Chromium and saves a screenshot; `--do <file.py>` clicks, hovers or asserts first
  (an exception exits non-zero, so the file doubles as a scripted check), `--omnigent` signs
  in to an Omnigent server with the CLI's login. Console errors and HTTP responses of 400 and
  up go to stderr. Every host with Playwright's Chromium in `~/.cache/ms-playwright` can run
  it; its docstring says what a sandbox needs.

Clean up what you started, and only that: a browser or server you launched, by the pid you
saved.
