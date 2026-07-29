from __future__ import annotations

import sys
from types import ModuleType

from tests.teaching import _script_runner


def test_in_process_script_restores_added_and_replaced_modules(tmp_path, monkeypatch) -> None:
    original = ModuleType("teaching_existing_probe")
    monkeypatch.setitem(sys.modules, "teaching_existing_probe", original)
    sys.modules.pop("teaching_added_probe", None)
    script = tmp_path / "mutate_modules.py"
    script.write_text(
        "import sys\n"
        "from types import ModuleType\n"
        "sys.modules['teaching_existing_probe'] = ModuleType('replacement')\n"
        "sys.modules['teaching_added_probe'] = ModuleType('added')\n",
        encoding="utf-8",
    )

    result = _script_runner.run(
        [sys.executable, script],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert sys.modules["teaching_existing_probe"] is original
    assert "teaching_added_probe" not in sys.modules
