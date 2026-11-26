#!/usr/bin/env python3
"""A Claude Code PreToolUse hook on Bash: refuse a command that prints the whole environment or a
secret variable's value (#2359).

A session's tool output lands in a transcript the agent server stores and shows in its UI, so a
secret printed there leaves the VM. A cloud environment carries secrets by design (the Codex login
`cloud_verifier.py seed` stores as ``CODEX_AUTH_JSON``, a Claude Code token, tokens a project adds),
and nothing redacts a transcript after the fact: this is the check in code. It reads the tool
call's JSON on stdin and refuses (exit 2, the reason on stderr, as Claude Code's PreToolUse
contract takes it) a command one of whose segments:

- prints the whole environment: ``env``, ``printenv``, ``set``, ``export``/``export -p``,
  ``declare -p``/``-x``, ``typeset``, ``compgen -e`` is not one (it lists names only), or reads
  ``/proc/<pid>/environ``; or an interpreter one-liner (``python -c``, ``node -e``, ...) that
  prints ``os.environ`` or ``process.env`` whole;
- prints a variable whose name looks like a secret (``*_TOKEN``, ``*_KEY``, ``*_SECRET``,
  ``*AUTH*``, ``*PASSWORD*``, ``*CREDENTIAL*``): ``printenv NAME``, ``echo "$NAME"``, ``printf``,
  ``declare -p NAME``, or an interpreter one-liner that prints it.

Output that goes into a hasher or a counter (``| wc -c``, ``| sha256sum``, ``| grep -q``), a
variable's length (``${#NAME}``), a test (``[ -n "$NAME" ]``), ``env FOO=1 cmd``, ``printenv HOME``
and every command that merely has the variable in its environment pass. The refusal says why and
how to check a variable without printing it. Anything it can't read or parse is allowed (exit 0),
so a bug here never blocks work. Stdlib only, no subprocess: it runs on every Bash call.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from heavy_run_guard import (
    HEREDOC,
    SEPARATORS,
    SHELL_DEPTH,
    SHELLS,
    drop_heredocs,
    shell_string,
    strip_leading,
)

# a name holds a secret when one of its `_`-parts is, or ends in, one of these words (so GH_TOKEN,
# GITHUBTOKEN, CODEX_AUTH_JSON and AWS_SECRET_ACCESS_KEY do; TOKENIZER, AUTHOR and SSH_AUTH_SOCK do not)
SECRET_ENDINGS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS", "APIKEY")
SECRET_PARTS = {"KEY", "AUTH", "AUTHORIZATION", "OAUTH"}
NOT_SECRET = {"SSH_AUTH_SOCK"}
# a command that reduces its input to a length or a digest, or only tests it: no value comes out
REDUCERS = {"wc", "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum", "shasum", "md5sum"}
REDUCERS |= {"cksum", "b3sum"}
QUIET_GREP = {"-q", "-c", "--quiet", "--silent", "--count", "-l", "-L"}
PRINTERS = {"echo", "printf", "print"}
# programs that print what they read; a command that only names /proc/<pid>/environ (echo, git,
# ls, a quiet grep) shows none of it
READERS = {"cat", "tac", "less", "more", "head", "tail", "strings", "xxd", "od", "hexdump", "tr"}
READERS |= {"cut", "paste", "sort", "uniq", "nl", "base64", "xargs", "awk", "gawk", "sed", "rev"}
READERS |= {"cp", "dd", "tee", "grep", "egrep", "fgrep", "rg", "ag", "ack", "column", "fold"}
# programs that run the rest of the line as a command, which strip_leading does not know
EXTRA_WRAPPERS = {"nice", "ionice", "stdbuf", "busybox", "setsid", "builtin", "chrt", "taskset"}
INTERPRETERS = re.compile(r"^(python[\d.]*|node|nodejs|deno|bun|perl|ruby|php|lua)$")
CODE_FLAGS = {"-c", "-e", "-p", "-r", "--eval", "--print", "-E"}
ENV_RUN = re.compile(r"/proc/[^\s'\"]*\benviron\b")  # also /proc/self/task/1/environ
# code that starts a command that dumps, or opens the file
# (`set`, `export` and `env` count only as the whole string or list, with nothing but options and
# NAME=value words after `env` (no command to run), before `-p`/`-0`, a pipe, a redirect (`2>&1`
# too) or `;`: `set -e; make` and `['env', 'FOO=1', 'ls']` run something else, #2429)
ENV_OPT = r"(?:-u\s+\w+|-[^\s'\"]+|\w+=[^\s'\"]*)"
ENV_ITEM = r"(?:-u['\"]\s*,\s*['\"]\w+|-[^\s'\",]+|\w+=[^'\",]*)"
CODE_EXEC = re.compile(
    r"(?:popen|system|check_output|check_call|\brun|\bcall|exec\w*|spawn\w*)\(\s*[\[(]?\s*['\"]"
    r"(?:printenv\b"
    rf"|env(?:\s+{ENV_OPT})*\s*['\"]\s*(?:[\])]|,(?!\s*['\"]))"
    rf"|env['\"]\s*(?:,\s*['\"]{ENV_ITEM}['\"]\s*)*[\])]"
    r"|(?:env|set|export)(?:\s*['\"]\s*(?:[\])]|,(?!\s*['\"]))|\s+-[0p]\b|\s*(?:\d*[<>]|[;&|\n])))"
)
# code that reduces a value to a length, a digest or a test, or only assigns the environment:
# the parts that leave no value in the output
CODE_REDUCED = re.compile(
    r"\blen\(|hashlib|\bsha\d+|\bmd5\b|\bbool\(|\bis\s+(?:not\s+)?None\b|[!=]=\s*None\b"
    r"|['\"]\s*(?:not\s+)?in\s+(?:(?:os\.)?environb?|process\.env)\b|\.length\b|\bhasOwnProperty\b"
)
CODE_ASSIGNED = re.compile(
    r"(?:=|\*\*|\.\.\.)\s*(?:dict\(\s*)?(?:(?:os\.)?environb?|process\.env)(?:\.copy\(\))?"
)
# `from os import environ`: the bare name then is the whole environment (#2429)
CODE_IMPORT_ENVIRON = re.compile(r"\bfrom\s+os\s+import\b[^\n;]*\benvironb?\b[^\n;]*")
CODE_BARE_ENVIRON = re.compile(
    r"(?<![\w.$])environb?\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b))"
)
CODE_STRING = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
# the first quoted name a lookup call takes: os.environ.get('NAME'), os.getenv("NAME"), ENV.fetch
CODE_GET = re.compile(
    r"(?:environ\.get|environ\.setdefault|getenv|env\.get|ENV\.fetch|ENV\.dig)\(\s*['\"]([A-Za-z0-9_]+)['\"]"
)
AWK_NAMED = re.compile(r"\bENVIRON\s*\[\s*[\"']([A-Za-z0-9_]+)[\"']")
AWK_ENV = re.compile(r"\bENVIRON\b(?!\s*\[\s*[\"'])")
# the whole environment inside code: os.environ / process.env / ENV / %ENV not indexed or looked up
CODE_WHOLE = re.compile(
    r"\bos\.environb?\b(?!\s*(?:\[|\.get\b|\.setdefault\b|\.pop\b))"
    r"|\bprocess\.env\b(?![\w.\[])"
    r"|\b(?:Deno\.env\.toObject|environ\.items|getenv\(\s*\))"
    r"|(?<![\w$])%ENV\b|\bENV\.(?:to_h|each|inspect|to_a|map)\b|\bENV(?![\w\[.])|\$_ENV\b|\$_SERVER\b|\bgetenv\(\)"
)
CODE_NAMED = re.compile(
    r"(?:environb?|getenv|process\.env|ENV|env\.get)\W{1,4}(?:b(?=['\"]))?\W?([A-Za-z0-9_]+)"
)
CODE_PRINTS = re.compile(
    r"print|console\.|echo|puts|write|say\b|dump|stringify|log\b|inspect|\bpp?\b"
)
# $NAME and ${NAME}, with ${NAME:-x}, ${NAME#x} and the like (which can print the value); not
# ${#NAME} (a length) nor ${NAME:+x} / ${NAME+x} (which print x, never the value)
VAR_USE = re.compile(
    r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{!?([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_]|:?\+)"
)

# curl options that print the request, headers included (a short flag among others: `-sv`)
CURL_VERBOSE = re.compile(r"--verbose|--trace[\w-]*|-[A-Za-z]*v[A-Za-z]*")
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
ASSIGN_VALUE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)", re.DOTALL)

HOW = (
    "Check a variable without printing it: `printenv NAME | wc -c` (its length), `printenv NAME |"
    ' sha256sum | cut -c1-8` (a short hash), `[ -n "${NAME-}" ] && echo set`, or list the names'
    " only with `compgen -e`. A transcript is stored by the agent server and shown in its UI, so a"
    " value printed there is no longer secret."
)


def secret(name: str) -> bool:
    if name in NOT_SECRET:
        return False
    if name != name.upper():  # environment names are upper case; `$key` is a loop variable
        return False
    parts = name.split("_")
    if "PUBLIC" in parts:  # PUBLIC_KEY holds no secret
        return False
    return any(p.endswith(SECRET_ENDINGS) or p in SECRET_PARTS for p in name.split("_"))


def pipelines(command: str) -> list[tuple[list[str], str, list[str]]]:
    """Each simple command's words, the separator that follows it ("" at the end) and the files it
    reads with ``<``, split as ``heavy_run_guard.segments`` splits them (a backtick substitution
    is a ``( … )`` group, a ``<(`` or ``$(`` token a separator). Raises ValueError on a quote it
    can't close."""
    text = re.sub(r"`([^`]*)`", r" ( \1 ) ", drop_heredocs(command))
    lex = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    parts: list[list[str]] = [[]]
    reads: list[list[str]] = [[]]
    seps: list[str] = []
    target = ""  # "<" or ">" while the next word is a redirection's file
    for token in lex:
        punct = bool(token) and set(token) <= set("();<>|&\n")  # an operator, never a quoted word
        if token in SEPARATORS or (
            punct and (set(token) <= set(";&|\n") or set(token) & set("()"))
        ):
            seps.append(token)
            parts.append([])
            reads.append([])
            target = ""
        elif token and set(token) <= set("<>&") and set(token) & set("<>"):
            if parts[-1] and parts[-1][-1].isdigit():
                parts[-1].pop()
            if token.endswith("<<<"):
                target = "h"  # a here-string: the next word is the command's input
            else:
                target = "<" if token.endswith("<") and "<<" not in token else ">"
        elif target:
            if target == "<":
                reads[-1].append(token)
            elif target == "h":
                reads[-1].append("<<<" + token)
            target = ""
        else:
            parts[-1].append(token)
    seps.append("")
    return [(p, s, r) for p, s, r in zip(parts, seps, reads) if p or r]


def reduces(words: list[str]) -> bool:
    """Whether a command turns its input into a length, a digest or an exit status."""
    rest, _, _ = strip_leading(words, None)
    if not rest:
        return False
    prog, args = Path(rest[0]).name, rest[1:]
    if prog in REDUCERS:
        return True
    if prog in ("grep", "egrep", "fgrep", "rg"):
        return any(a in QUIET_GREP or re.fullmatch(r"-[a-zA-Z]*[qc][a-zA-Z]*", a) for a in args)
    if prog == "openssl":
        return args[:1] == ["dgst"]
    return False


def names_used(words: list[str], tainted: frozenset = frozenset()) -> list[str]:
    """The secret-shaped variables a command line reads with ``$NAME`` or ``${NAME}``, never
    ``${#NAME}`` (a length). ``tainted``: names assigned from a secret earlier in the command."""
    found: list[str] = []
    for word in words:
        for m in VAR_USE.finditer(word):
            name = m.group(1) or m.group(2)
            if name and (secret(name) or name in tainted):
                found.append(name)
    return found


def judge_words(
    rest: list[str], reduced: bool, stdin: str = "", tainted: frozenset = frozenset()
) -> str | None:
    """The refusal for one command (its leading words stripped), else None. ``reduced``: its output
    goes into a hasher or counter; ``stdin``: the whole command line, the code an interpreter with
    no script reads from a here-document."""
    prog, args = Path(rest[0]).name, rest[1:]
    plain = [a for a in args if not a.startswith("-")]
    flags = "".join(a[1:] for a in args if re.fullmatch(r"-[A-Za-z]+", a))
    # a search's first plain word is its pattern, which names no file it reads
    files = plain[1:] if prog in ("grep", "egrep", "fgrep", "rg", "ag", "ack") else args
    if ENV_RUN.search(" ".join(files)) and prog in READERS and not reduced:
        quiet = prog in ("grep", "egrep", "fgrep", "rg") and any(
            a in QUIET_GREP or re.fullmatch(r"-[a-zA-Z]*[qc][a-zA-Z]*", a) for a in args
        )
        if not quiet:
            return "it reads a process's whole environment (/proc/<pid>/environ)"
    if prog in ("awk", "gawk") and not reduced:
        program = " ".join(args)
        if AWK_ENV.search(program):
            return f"`{prog}` prints the whole environment (ENVIRON)"
        if any(secret(n) for n in AWK_NAMED.findall(program)):
            return f"`{prog}` prints ENVIRON of a variable that looks like a secret"
    if prog == "printenv":
        if reduced:
            return None
        named = [a for a in plain]
        if not named:
            return "`printenv` with no variable named prints the whole environment"
        if bad := [n for n in named if secret(n)]:
            return f"`printenv {bad[0]}` prints a secret's value"
        return None
    if prog in ("set",) and not args:
        return None if reduced else "`set` with no argument prints every variable and function"
    if prog == "export" and (not args or "p" in flags and not plain):
        return None if reduced else "`export -p` prints every exported variable"
    if prog in ("declare", "typeset"):
        dump = not plain and ("p" in flags or "x" in flags or not args)
        if dump:
            return None if reduced else f"`{prog}` with no variable named prints every variable"
        if "p" in flags and (bad := [n for n in plain if secret(n)]) and not reduced:
            return f"`{prog} -p {bad[0]}` prints a secret's value"
    if prog == "env" and not plain and not any("=" in a for a in args):
        return None if reduced else "`env` with no command prints the whole environment"
    if prog in PRINTERS and not reduced and (bad := names_used(args, tainted)):
        return f"`{prog}` prints ${bad[0]}, which looks like a secret"
    if (
        prog == "curl"
        and any(CURL_VERBOSE.fullmatch(a) for a in args)
        and names_used(args, tainted)
    ):
        return "`curl` in verbose or trace mode prints the request headers, a secret included"
    if INTERPRETERS.match(prog) and not reduced:
        code = ""
        for i, a in enumerate(args):
            if a in CODE_FLAGS or re.fullmatch(r"-[a-zA-Z]*[ceE]", a):
                code = " ".join(args[i + 1 :])
                break
        else:
            if not plain:  # no script file: it reads its code from stdin (a heredoc or a pipe)
                code = stdin
        if code:
            if ENV_RUN.search(code) or CODE_EXEC.search(code):
                return f"`{prog}` code reads or dumps the whole environment"
            if CODE_PRINTS.search(code) and not CODE_REDUCED.search(code):
                bare = CODE_ASSIGNED.sub("=", CODE_STRING.sub("''", code))
                if CODE_IMPORT_ENVIRON.search(bare):
                    bare = CODE_BARE_ENVIRON.sub("os.environ", CODE_IMPORT_ENVIRON.sub("", bare))
                if CODE_WHOLE.search(bare):
                    return f"`{prog}` code prints the whole environment"
                named = CODE_NAMED.findall(code) + CODE_GET.findall(code)
                if any(secret(n) for n in named):
                    return f"`{prog}` code prints a variable that looks like a secret"
    return None


def unwrap(rest: list[str]) -> list[str]:
    """``rest`` without leading wrappers ``strip_leading`` does not know (``nice``, ``busybox``),
    with their options."""
    while rest and Path(rest[0]).name in EXTRA_WRAPPERS:
        i = 1
        while i < len(rest) and (rest[i].startswith("-") or rest[i].isdigit()):
            i += 1
        rest = rest[i:]
        if rest and Path(rest[0]).name != "env":  # `nice env` is a dump, judged as `env`
            rest, _, _ = strip_leading(rest, None)
    return rest


def find_commands(rest: list[str]) -> list[str]:
    """The command lines a ``find`` runs with ``-exec``, ``-execdir``, ``-ok`` or ``-okdir``
    (up to ``;`` or ``+``, without the ``{}`` placeholder)."""
    found: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] in FIND_EXEC:
            j = i + 1
            while j < len(rest) and rest[j] not in (";", "+", "\\;"):
                j += 1
            if inner := [w for w in rest[i + 1 : j] if w != "{}"]:
                found.append(shlex.join(inner))
            i = j
        i += 1
    return found


def taint(words: list[str], tainted: frozenset) -> frozenset:
    """``tainted`` plus every name ``words`` assign from a secret variable (``x=$GITHUB_TOKEN``,
    also after ``export``, ``declare``, ``local`` or ``readonly``); a name assigned from
    anything else is no longer tainted."""
    out = set(tainted)
    for word in words:
        m = ASSIGN_VALUE.fullmatch(word)
        if m and names_used([m.group(2)], tainted):
            out.add(m.group(1))
        elif m:  # a plain reassignment clears the name
            out.discard(m.group(1))
    return frozenset(out)


def heredoc_bodies(command: str) -> list[str]:
    """The bodies of ``command``'s here-documents (what ``drop_heredocs`` removes)."""
    bodies: list[str] = []
    ending: str | None = None
    for line in command.split("\n"):
        if ending is not None:
            if line.strip() == ending:
                ending = None
            else:
                bodies[-1] += line + "\n"
            continue
        if found := HEREDOC.search(line.replace("<<<", "   ")):
            ending = found.group(2)
            bodies.append("")
    return bodies


def stdin_shell(rest: list[str]) -> bool:
    """Whether ``rest`` is a shell that reads its commands from stdin (no ``-c``, no script)."""
    return (
        Path(rest[0]).name in SHELLS
        and shell_string(rest) is None
        and not [a for a in rest[1:] if not a.startswith("-")]
    )


def judge_stdin_shell(
    items: list, i: int, reads: list[str], command: str, depth: int, tainted: frozenset
) -> str | None:
    """The refusal for a shell at ``items[i]`` that runs what is fed to it: a here-string, a
    here-document or the echo before a pipe."""
    if depth >= SHELL_DEPTH:
        return None
    fed = [r[3:] for r in reads if r.startswith("<<<")]
    fed += heredoc_bodies(command)
    if i and items[i - 1][1] in ("|", "|&"):
        before, _, _ = strip_leading(items[i - 1][0], None)
        if before and Path(before[0]).name in PRINTERS | {"cat"}:
            fed.append(" ".join(before[1:]))
    for text in fed:
        if why := judge(text, depth + 1, tainted):
            return why
    return None


def judge(command: str, depth: int = 0, tainted: frozenset = frozenset()) -> str | None:
    """The reason ``command`` is refused, else None. ``tainted``: names assigned from a secret
    variable by an outer command line."""
    items = pipelines(command)
    for i, (words, _sep, reads) in enumerate(items):
        # a segment whose output a later segment of its pipeline reduces to a length or a digest
        j, reduced = i, False
        while j < len(items) and items[j][1] in ("|", "|&"):
            j += 1
            reduced = reduced or reduces(items[j][0])
        if not reduced and any(ENV_RUN.search(r) for r in reads):
            return "it reads a process's whole environment (/proc/<pid>/environ)"
        rest, _, _ = strip_leading(words, None)
        prefix = words[: len(words) - len(rest)]
        tainted = taint(prefix, tainted)
        rest = unwrap(rest)
        if rest and Path(rest[0]).name in ("export", "declare", "typeset", "local", "readonly"):
            tainted = taint(rest[1:], tainted)
        if rest and Path(rest[0]).name == "unset":
            tainted = tainted - set(rest[1:])
        if not rest:
            if any(Path(w).name == "env" for w in prefix) and not reduced:
                return "`env` with no command prints the whole environment"
            continue
        if Path(rest[0]).name == "eval":
            if depth < SHELL_DEPTH and (why := judge(" ".join(rest[1:]), depth + 1, tainted)):
                return why
            continue
        if (inner := shell_string(rest)) is not None:
            if depth < SHELL_DEPTH and (why := judge(inner, depth + 1, tainted)):
                return why
            continue
        if stdin_shell(rest) and not reduced:
            if why := judge_stdin_shell(items, i, reads, command, depth, tainted):
                return why
            continue
        if Path(rest[0]).name == "find" and not reduced and depth < SHELL_DEPTH:
            for found in find_commands(rest):
                if why := judge(found, depth + 1, tainted):
                    return why
        fed = [r[3:] for r in reads if r.startswith("<<<")]
        if fed and not reduced and Path(rest[0]).name in READERS and names_used(fed, tainted):
            return f"`{Path(rest[0]).name}` prints ${names_used(fed, tainted)[0]}, which looks like a secret"
        if why := judge_words(rest, reduced, command, tainted):
            return why
    return None


def verdict(payload: dict) -> tuple[int, str]:
    """The hook's exit code and its stderr line for one tool call."""
    if payload.get("tool_name") not in (None, "Bash"):
        return 0, ""
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0, ""
    why = judge(command)
    if why is None:
        return 0, ""
    return 2, f"secret_env_guard: refused, {why}. {HOW}"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        code, line = verdict(payload) if isinstance(payload, dict) else (0, "")
    except Exception:  # noqa: BLE001  (a parse bug must never block a command)
        return 0
    if line:
        print(line, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
