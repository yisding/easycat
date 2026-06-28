"""Chapter 11 entry point — delegates to investigate.py.

This chapter has two scripts:
  generate_bundles.py — run once to create the planted-bug bundles
  investigate.py      — query those bundles interactively (start here
                        if the bundles/ directory already exists)

Run with no arguments to inspect the first planted-bug bundle. Pass a
bundle path (and optional --stage/--name/--limit filters) to point
investigate.py at a different fixture.
"""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    here = Path(__file__).parent
    # investigate.py needs a positional bundle path. Default to the first
    # planted-bug fixture so the shared `.../main.py` entry point runs
    # without surprises; forward any explicit args through untouched.
    if len(sys.argv) == 1:
        sys.argv.append(str(here / "bundles" / "bug_01_empty_final.bundle"))
    runpy.run_path(str(here / "investigate.py"), run_name="__main__")
