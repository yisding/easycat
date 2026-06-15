"""M6b import-weight + side-effect boundary guards for ``easycat.planning``.

The planner must report missing env/extras WITHOUT instantiating providers or
importing a heavy SDK. ``import easycat.planning`` must not pull aiohttp or any
heavy provider SDK, and ``build_provider_plan`` must be side-effect-free.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_planning_does_not_pull_aiohttp_or_heavy_sdk() -> None:
    # Run in a FRESH subprocess so the check observes a true module-load, not
    # leftover sys.modules state from sibling tests.
    code = (
        "import sys; import easycat.planning; "
        "heavy = sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'aiohttp','aiortc','openai','livekit','onnxruntime',"
        "'torch','deepgram','twilio'}); "
        "print(heavy)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_importing_planning_does_not_pull_config_factory() -> None:
    code = "import sys; import easycat.planning; print('easycat.config._factory' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stdout


def test_build_plan_does_not_instantiate_providers() -> None:
    # Building a plan with the silero VAD selected (whose probe is installed)
    # must NOT import the provider SDK (onnxruntime) — it only find_spec's it.
    code = (
        "import sys; "
        "from easycat.planning import build_provider_plan; "
        "from easycat.project.schema import VoiceProfile; "
        "p = VoiceProfile(name='default', transport='local', vad='silero'); "
        "build_provider_plan(p, environ={'OPENAI_API_KEY': 'x'}); "
        "print('onnxruntime' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stdout


def test_planning_is_submodule_only_no_top_level_export() -> None:
    # ``easycat.planning`` is a submodule export and must NOT appear in
    # ``easycat.__all__`` (only top-level VoiceApp counts against the cap).
    import easycat

    assert "planning" not in easycat.__all__
    assert "build_provider_plan" not in easycat.__all__
    assert "ProviderPlan" not in easycat.__all__
