#!/usr/bin/env -S uv run -q --script
# /// script
# dependencies = ["playwright==1.63.0"]
# ///
"""engineering-loop/scripts/browse.py: the browser's former path, which the `browsing` skill's scripts/browse.py is
now. Kept for one release line, so a project pinned to engineering keeps its commands: it runs that script with the
same arguments, input and exit code, after one deprecation line on stderr. It goes away with the release that drops
engineering's dependencies on the packages split out of it."""

import runpy
import sys
from pathlib import Path

# a forwarder, never a module: an import that found it took the wrong folder
if __name__ != "__main__":
    raise ImportError(f"{__file__} only forwards to another skill's script: import that script")


def installed(skill: str) -> Path:
    """The scripts folder of `skill`, which another package ships, found by the host's install layout: a sibling
    skill folder (APM's one skills directory), a sibling plugin's (this repo, or a marketplace Claude Code loads in
    place), else the newest version of a plugin in Claude Code's cache (`<marketplace>/<plugin>/<version>/`) that
    no update has orphaned. Exits naming the skill to install when none holds it."""
    # ponytail: three layouts, the ones APM and Claude Code use; a host with another adds its glob here.
    own = Path(__file__).resolve().parents[1]
    plugin = own.parent.parent
    cached = plugin.parent.parent.glob(f"[!.]*/[!.]*/skills/{skill}")
    for folder in [
        own.parent / skill,
        *sorted(plugin.parent.glob(f"[!.]*/skills/{skill}")),
        *sorted(cached, key=lambda path: path.stat().st_mtime, reverse=True),
    ]:
        if (folder / "SKILL.md").is_file() and not (folder.parents[1] / ".orphaned_at").exists():
            return folder / "scripts"
    sys.exit(f"{Path(__file__).name} needs the `{skill}` skill: install the package that ships it")


skill = "browsing"
target = installed(skill) / "browse.py"
print(
    f"engineering-loop/scripts/browse.py is deprecated: run the `{skill}` skill's scripts/{target.name}",
    file=sys.stderr,
)
# as if the target ran itself, so no forwarder beside this one is importable
sys.path[0] = str(target.parent)
runpy.run_path(str(target), run_name="__main__")
