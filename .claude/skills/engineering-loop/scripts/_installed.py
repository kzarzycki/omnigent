"""Where a skill that another package ships is installed: the one `installed()` every script of engineering-loop
uses, so the layouts it knows cannot drift between copies. A script loads it by path from its own folder, since a
script may be imported from anywhere and a forwarder runs before anything of its own.
All copies are identical. A copy in a forwarder folder (engineering's) only forwards to another skill's script
for the forwarders beside it, which run nothing of their own."""

import sys
from pathlib import Path


def installed(skill: str) -> Path:
    """The scripts folder of `skill`, which another package ships, found by the host's install layout: a sibling
    skill folder (APM's one skills directory), a sibling plugin's (this repo, or a marketplace Claude Code loads in
    place), a native plugin APM deployed to the project's `.agents/plugins/`, else the newest version of a plugin in
    Claude Code's cache (`<marketplace>/<plugin>/<version>/`) that no update has orphaned. Exits naming the skill to
    install when none holds it."""
    # ponytail: four layouts, the ones APM and Claude Code use; a host with another adds its glob here.
    own = Path(__file__).resolve().parents[1]
    plugin = own.parent.parent
    cached = plugin.parent.parent.glob(f"[!.]*/[!.]*/skills/{skill}")
    for folder in [
        own.parent / skill,
        *sorted(plugin.parent.glob(f"[!.]*/skills/{skill}")),
        *sorted(plugin.parent.glob(f".agents/plugins/*/skills/{skill}")),
        *sorted(cached, key=lambda path: path.stat().st_mtime, reverse=True),
    ]:
        if (folder / "SKILL.md").is_file() and not (folder.parents[1] / ".orphaned_at").exists():
            return folder / "scripts"
    sys.exit(
        f"{Path(sys.argv[0]).name} needs the `{skill}` skill: install the package that ships it"
    )
