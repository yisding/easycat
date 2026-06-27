"""Chapter 11 entry point — delegates to investigate.py.

This chapter has two scripts:
  generate_bundles.py — run once to create the planted-bug bundles
  investigate.py      — query those bundles interactively (start here
                        if the bundles/ directory already exists)
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "investigate.py"), run_name="__main__")
