from __future__ import annotations

from tests.install._install_guidance_helpers import (
    MARKDOWN_PREREQS_RE,
    PROVIDER_EXTRA_BY_ENV_VAR,
    REPO_ROOT,
    ast,
)


def test_teaching_ladder_prerequisites_run_doctor_after_setup() -> None:
    """The teaching overview should send readers through the first-run preflight."""
    readme = (REPO_ROOT / "docs" / "teaching" / "README.md").read_text(encoding="utf-8")
    prerequisites = readme.split("## Prerequisites", 1)[1].split("## Conventions", 1)[0]

    local_index = prerequisites.index("uv sync --extra local --group dev")
    sync_index = prerequisites.index("uv sync --extra quickstart --group dev")
    key_index = prerequisites.index("OPENAI_API_KEY")
    doctor_index = prerequisites.index("uv run easycat doctor")

    assert local_index < sync_index < key_index < doctor_index


def test_teaching_chapter_key_prerequisites_run_doctor() -> None:
    """Self-contained chapter READMEs with API keys should repeat the preflight."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.glob("[0-9][0-9]-*/README.md")):
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        if "API_KEY" in section and "uv run easycat doctor" not in section:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, "Teaching chapter prerequisites missing doctor preflight:\n" + "\n".join(
        missing
    )


def test_teaching_provider_prerequisites_warn_about_live_usage_costs() -> None:
    """Provider-backed lessons should set billing expectations before learners run them."""
    teaching_root = REPO_ROOT / "docs" / "teaching"
    overview = (teaching_root / "README.md").read_text(encoding="utf-8")
    overview_prerequisites = " ".join(
        overview.split("## Prerequisites", 1)[1].split("## Conventions", 1)[0].split()
    )
    missing: list[str] = []

    if "may incur charges" not in overview_prerequisites:
        missing.append("docs/teaching/README.md: charge warning")
    if "billing and usage limits" not in overview_prerequisites:
        missing.append("docs/teaching/README.md: billing limits")

    for path in sorted(teaching_root.glob("[0-9][0-9]-*/README.md")):
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        if "API_KEY" not in section:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "may incur charges" not in section:
            missing.append(f"{rel}: charge warning")
        if "billing and usage limits" not in section:
            missing.append(f"{rel}: billing limits")

    assert not missing, "Teaching provider cost guidance is incomplete:\n" + "\n".join(missing)


def test_teaching_provider_prerequisites_warn_about_data_handling() -> None:
    """Provider-backed lessons should keep sensitive data out of live exercises."""
    teaching_root = REPO_ROOT / "docs" / "teaching"
    overview = (teaching_root / "README.md").read_text(encoding="utf-8")
    overview_prerequisites = overview.split("## Prerequisites", 1)[1].split("## Conventions", 1)[0]
    missing: list[str] = []

    if "non-sensitive test content" not in overview_prerequisites:
        missing.append("docs/teaching/README.md: non-sensitive content")
    if "provider data-handling policies" not in overview_prerequisites:
        missing.append("docs/teaching/README.md: provider policies")

    for path in sorted(teaching_root.glob("[0-9][0-9]-*/README.md")):
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        if "API_KEY" not in section:
            continue
        normalized_section = " ".join(section.split())
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "non-sensitive test" not in normalized_section:
            missing.append(f"{rel}: non-sensitive test data")
        if "provider data-handling policies" not in normalized_section:
            missing.append(f"{rel}: provider policies")

    assert not missing, "Teaching provider data guidance is incomplete:\n" + "\n".join(missing)


def test_teaching_provider_key_setup_names_required_extras() -> None:
    """Provider-key setup snippets should include the matching optional extra."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "Dependencies:" not in doc:
            continue
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in doc and extra not in doc:
                missing.append(f"{rel}: {env_var} setup missing {extra}")

    readme_paths = sorted({*teaching_root.rglob("README.md"), teaching_root / "README.md"})
    for path in readme_paths:
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in section and extra not in section:
                missing.append(f"{rel}: {env_var} prerequisites missing {extra}")

    assert not missing, "Teaching setup docs missing provider extras:\n" + "\n".join(missing)
