#!/usr/bin/env python3
"""PreToolUse hook of the landing environment: refuse tool calls that would show the
environment or the signing key, so neither reaches the session's transcript.

It acts only where OMNIGENT_LANDING_ENVIRONMENT=1, a variable only the landing
environment sets (landing-env.py), and passes every call elsewhere. Exit 2 refuses
the call and tells the agent why on stderr. `land` reads the key itself.
"""

import json
import os
import re
import shlex
import sys

MARKER = "OMNIGENT_LANDING_ENVIRONMENT"
# Searched in a call's whole input, whichever tool makes it: /proc/<pid>/environ,
# os.environ, awk's ENVIRON, Node's process.env, getenv(), and the key's variable.
TEXT_RULES = [
    (re.compile(r"environ\b", re.IGNORECASE), "reads the process environment"),
    (re.compile(r"\bprocess\.env\b|\bgetenv\b"), "reads the process environment"),
    (re.compile(r"SIGNING_KEY", re.IGNORECASE), "names the signing key's variable"),
    (re.compile(r"\$\{!"), "expands variables by name"),
]
PRINTERS = {"env", "printenv"}
# Bash builtins that list every variable when given only flags (`set`: no arguments).
LISTERS = {"export", "declare", "typeset", "readonly"}
WRAPPERS = {"sudo", "exec", "command", "builtin", "nohup", "time", "nice", "xargs", "timeout", "stdbuf"}
SEPARATORS = {";", "&", "&&", "|", "||", "(", ")", "`", "$(", "\n", "{", "}", "!"}


def tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()`")
    lexer.whitespace = " \t\r"
    lexer.wordchars += "$!{}\\"
    try:
        return list(lexer)
    except ValueError:  # an unbalanced quote: split on whitespace and separators instead
        return [part for part in re.split(r"[\s;&|()`]+", command) if part]


def command_words(command: str) -> list[list[str]]:
    """Each simple command's words, with quoted words that hold a command searched too."""
    found: list[list[str]] = []
    current: list[str] = []
    for token in tokens(command):
        if token in SEPARATORS or set(token) <= set(";&|()`\n"):
            found.append(current)
            current = []
            continue
        if token.startswith("$("):
            found.append(current)
            current, token = [], token[2:]
        current.append(token)
        if re.search(r"[\s;&|`]|\$\(", token):  # bash -c '...', a quoted pipeline
            found.extend(command_words(token))
    found.append(current)
    return [words for words in found if words]


def refusal(words: list[str]) -> str | None:
    index = 0
    while index < len(words) and (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[index])
        or os.path.basename(words[index]) in WRAPPERS
        or (index and words[index].startswith("-"))
        or (index and words[index - 1] == "timeout")
    ):
        index += 1
    for word in words[index:]:
        if os.path.basename(word) in PRINTERS:
            return f"runs `{os.path.basename(word)}`, which prints the environment"
    rest = words[index:]
    if rest and (
        rest == ["set"]
        or (rest[0] in LISTERS and all(arg.startswith("-") for arg in rest[1:]))
        or (rest[0] == "compgen" and any(re.fullmatch(r"-\w*[ev]\w*|-A", arg) for arg in rest[1:]))
    ):
        return f"runs `{' '.join(rest)}`, which lists every variable"
    if rest and os.path.basename(rest[0]) == "ps" and any(
        not arg.startswith("-") and "e" in arg for arg in rest[1:]
    ):
        return "runs `ps` with `e`, which prints each process's environment"
    return None


def check(tool_input: object) -> str | None:
    text = json.dumps(tool_input, ensure_ascii=False)
    for pattern, why in TEXT_RULES:
        if pattern.search(text):
            return why
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, str):
        for words in command_words(command):
            if why := refusal(words):
                return why
    return None


def main() -> int:
    if os.environ.get(MARKER) != "1":
        return 0
    try:
        call = json.load(sys.stdin)
    except ValueError:
        print("landing guard: refused: the tool call could not be read", file=sys.stderr)
        return 2
    if why := check(call.get("tool_input", {})):
        print(
            f"landing guard: refused, since this call {why}. A landing session never shows "
            "the environment or the signing key; `land` reads the key itself.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
