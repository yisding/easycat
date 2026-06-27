"""Chapter 2 entry point — delegates to streaming.py.

See streaming.py for the full implementation, or batch.py for the
single-call batch variant.
"""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "streaming.py"), run_name="__main__")
