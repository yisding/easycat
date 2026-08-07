from __future__ import annotations

import re
import shlex
from pathlib import Path


def pytest_target_tokens(command: str) -> list[str]:
    targets: list[str] = []
    options_with_values = {"-k", "-m", "-n", "-o", "--dist", "--cov-report"}

    for segment in re.split(r"\s+&&\s+", command):
        tokens = shlex.split(segment)
        if tokens[:3] != ["uv", "run", "pytest"]:
            continue

        index = 3
        while index < len(tokens):
            token = tokens[index]
            if token in options_with_values:
                index += 2
                continue
            if token.startswith("-"):
                index += 1
                continue
            targets.append(token)
            index += 1

    return targets


def pytest_target_problems(command: str, *, repo_root: Path, label: str) -> list[str]:
    problems: list[str] = []

    for target in pytest_target_tokens(command):
        file_target, _, node_id = target.partition("::")
        path = repo_root / file_target
        if not path.exists():
            problems.append(f"{label}: missing pytest target {file_target}")
            continue
        if not node_id:
            continue

        text = path.read_text(encoding="utf-8")
        for node_part in node_id.split("::"):
            node_name = node_part.split("[", 1)[0]
            if node_name.startswith("Test"):
                pattern = rf"\bclass\s+{re.escape(node_name)}\b"
            else:
                pattern = rf"\bdef\s+{re.escape(node_name)}\b"
            if re.search(pattern, text) is None:
                problems.append(f"{label}: missing pytest node {target}")
                break

    return problems
