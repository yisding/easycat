from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_easycat_bundle_fixture_loads_bundle_stack_only_when_called(tmp_path: Path) -> None:
    test_file = tmp_path / "test_lazy_easycat_plugin.py"
    test_file.write_text(
        """
import sys


def test_bundle_loader_is_lazy(easycat_bundle):
    assert callable(easycat_bundle)
    assert "easycat.debug.testing" not in sys.modules
    assert "easycat.debug._bundle_loader" not in sys.modules

    try:
        easycat_bundle("missing.bundle")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing bundle unexpectedly loaded")

    assert "easycat.debug.testing" in sys.modules
    assert "easycat.debug._bundle_loader" in sys.modules
""".lstrip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
