"""Normalize the package registry in ``ap-web/package-lock.json`` to public npm.

Local ``npm install`` runs resolve against whatever registry is configured
on the developer's machine (e.g. the Databricks npm proxy via
``NPM_CONFIG_REGISTRY`` or ``~/.npmrc``). When npm resolves a package
freshly — a new dependency, or a from-scratch ``rm package-lock.json &&
npm install`` — it bakes that registry's host into the ``resolved`` URL of
every affected entry. For this OSS repo the committed lockfile must always
point at public npm (``https://registry.npmjs.org``) so the lock is
reproducible for contributors who don't have the proxy — CI already pins
``NPM_CONFIG_REGISTRY: https://registry.npmjs.org/`` for the same reason.

Only the *host* of a proxy ``resolved`` URL is rewritten; the package path
is preserved (``…/-/foo-1.2.3.tgz`` is identical on the proxy and on public
npm). Non-proxy hosts are left untouched, so a future git / GitHub-tarball
dependency is never clobbered — only hosts matching :data:`_PROXY_HOST_MARKER`
are normalized.

This is a pre-commit *fixer*: it rewrites the URLs in place and exits
non-zero when it changed anything, so the commit aborts and the developer
re-stages the normalized lockfile (mirroring ``end-of-file-fixer`` and
friends, and the sibling ``normalize_uv_lock_registry.py``).

Pass ``--check`` to validate without writing: it exits non-zero (and names
the offending hosts) when a file still carries proxy URLs, but leaves it
untouched. CI runs this mode against the committed lockfile as a backstop
to the pre-commit hook — the ``package-lock.json`` freshness gate cannot
catch a proxy URL, because npm preserves an existing ``resolved`` host on
re-resolve rather than rewriting it to the configured registry.

Usage::

    python scripts/normalize_npm_lock_registry.py ap-web/package-lock.json          # fix
    python scripts/normalize_npm_lock_registry.py --check ap-web/package-lock.json   # verify
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# The canonical public registry host the committed lockfile must always use.
_CANONICAL_HOST = "registry.npmjs.org"

# A host is treated as a (Databricks) proxy to rewrite when it contains this
# marker. Kept deliberately narrow so only proxy mirrors are normalized and
# legitimate non-npm sources (git, GitHub tarballs) are never touched.
_PROXY_HOST_MARKER = "databricks"

# Captures the URL inside a package-lock ``"resolved": "<url>"`` entry.
_RESOLVED_RE = re.compile(r'("resolved":\s*")(https?://[^"]+)(")')


def _proxy_resolved_urls(text: str) -> list[str]:
    """Return the ``resolved`` URLs in *text* that point at a proxy host.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: Each proxy ``resolved`` URL, in order, duplicates preserved.
    """
    out: list[str] = []
    for m in _RESOLVED_RE.finditer(text):
        host = urlsplit(m.group(2)).hostname or ""
        if _PROXY_HOST_MARKER in host:
            out.append(m.group(2))
    return out


def normalize_text(text: str) -> str:
    """Return *text* with every proxy ``resolved`` host rewritten to public npm.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: The same text with each proxy ``resolved`` URL's host replaced
        by :data:`_CANONICAL_HOST`; the scheme is forced to ``https`` and the
        path/query/fragment are preserved. Non-proxy URLs are unchanged.
    """

    def _sub(m: re.Match[str]) -> str:
        parts = urlsplit(m.group(2))
        if _PROXY_HOST_MARKER not in (parts.hostname or ""):
            return m.group(0)
        fixed = urlunsplit(
            ("https", _CANONICAL_HOST, parts.path, parts.query, parts.fragment)
        )
        return f"{m.group(1)}{fixed}{m.group(3)}"

    return _RESOLVED_RE.sub(_sub, text)


def main(argv: list[str]) -> int:
    """Normalize (or, with ``--check``, validate) each given lockfile.

    :param argv: Filenames to process, optionally preceded/followed by the
        ``--check`` flag (passed by pre-commit or CI).
    :returns: In fix mode, ``1`` when a file was modified (so the commit
        aborts and the change is re-staged) else ``0``. In ``--check`` mode,
        ``1`` when any file still carries proxy URLs (printing the offending
        hosts) else ``0``; no file is written.
    """
    check = "--check" in argv
    files = [a for a in argv if a != "--check"]

    if check:
        ok = True
        for name in files:
            offenders = _proxy_resolved_urls(Path(name).read_text())
            if offenders:
                ok = False
                hosts = sorted({urlsplit(u).hostname or "" for u in offenders})
                print(
                    f"{name}: {len(offenders)} proxy registry URL(s) "
                    f"(expected host {_CANONICAL_HOST}): {', '.join(hosts)}"
                )
                print(
                    "Fix with: python scripts/normalize_npm_lock_registry.py "
                    f"{name} && git add {name}"
                )
        return 0 if ok else 1

    changed = False
    for name in files:
        path = Path(name)
        original = path.read_text()
        normalized = normalize_text(original)
        if normalized != original:
            path.write_text(normalized)
            print(f"{name}: normalized registry host to {_CANONICAL_HOST}")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
