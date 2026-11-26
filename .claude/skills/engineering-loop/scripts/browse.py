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

# a forwarder, never a module: an import that found it took the wrong folder
if __name__ != "__main__":
    raise ImportError(f"{__file__} only forwards to another skill's script: import that script")

from _installed import installed

skill = "browsing"
target = installed(skill) / "browse.py"
print(
    f"engineering-loop/scripts/browse.py is deprecated: run the `{skill}` skill's scripts/{target.name}",
    file=sys.stderr,
)
# as if the target ran itself, so no forwarder beside this one is importable
sys.path[0] = str(target.parent)
# the target loads its own `_installed`, not this folder's
sys.modules.pop("_installed", None)
runpy.run_path(str(target), run_name="__main__")
