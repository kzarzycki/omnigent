#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""engineering-loop/scripts/gatekeeper.py: the gatekeeper's former path, which the `gatekeeper` skill's scripts/gatekeeper.py is now. `brief` and `release` are the `engineering-loop` skill's scripts/loop.py.
Kept for one release line, so a project pinned to engineering keeps its commands, in a checkout of this repo as in
an install: it runs that script with the same arguments, input and exit code, after one deprecation line on stderr.
It goes away with the release that drops engineering's dependencies on the packages split out of it."""

import runpy
import sys

# a forwarder, never a module: an import that found it took the wrong folder
if __name__ != "__main__":
    raise ImportError(f"{__file__} only forwards to another skill's script: import that script")

from _installed import installed

# `brief` and `release` decide nothing, so they left the gatekeeper for the loop's own script
skill = "engineering-loop" if sys.argv[1:2] in (["brief"], ["release"]) else "gatekeeper"
target = installed(skill) / ("loop.py" if skill == "engineering-loop" else "gatekeeper.py")
print(
    f"engineering-loop/scripts/gatekeeper.py is deprecated: run the `{skill}` skill's scripts/{target.name}",
    file=sys.stderr,
)
# as if the target ran itself, so no forwarder beside this one is importable
sys.path[0] = str(target.parent)
# the target loads its own `_installed`, not this folder's
sys.modules.pop("_installed", None)
runpy.run_path(str(target), run_name="__main__")
