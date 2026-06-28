"""Chapter 9 entry point — delegates to ignore.py (the baseline).

This chapter has three scripts that build on each other:
  ignore.py   (9a) — bot ignores barge-in and finishes speaking
  cancel.py   (9b) — bot stops when user speaks
  estimate.py (9c) — bot stops and reports how far it got
Run them in order to see each incremental change.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "ignore.py"), run_name="__main__")
