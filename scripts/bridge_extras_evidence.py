"""Select and attest the real-SDK bridge contract for an extras-matrix cell."""

from __future__ import annotations

import argparse
import json
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any

BRIDGE_CONTRACT_NODES = {
    "langchain": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealLangChainBridgeContract"
    ),
    "langchain-v0": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealLangChainBridgeContract"
    ),
    "langgraph": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealLangGraphBridgeContract"
    ),
    "llama-agents": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealLlamaAgentsBridgeContract"
    ),
    "openai-agents": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealOpenAIAgentsBridgeContract"
    ),
    "pydantic-ai": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealPydanticAIBridgeContract"
    ),
    "pydantic-ai-v2": (
        "tests/integrations/agents/test_real_sdk_bridge_contracts.py"
        "::TestRealPydanticAIBridgeContract"
    ),
}


def contract_node(extra: str) -> str | None:
    """Return the exact public contract node exercised by *extra*, if any."""
    return BRIDGE_CONTRACT_NODES.get(extra)


def junit_counts(path: Path) -> dict[str, int]:
    """Return aggregate pytest JUnit counts without depending on pytest internals."""
    root = ET.parse(path).getroot()
    suites = [root] if root.tag.rsplit("}", 1)[-1] == "testsuite" else list(root)
    counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        if suite.tag.rsplit("}", 1)[-1] != "testsuite":
            continue
        for name in counts:
            counts[name] += int(suite.attrib.get(name, "0"))
    return counts


def build_evidence(
    *,
    extra: str,
    expected_sha: str,
    checkout_sha: str,
    junit_path: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Build an evidence record and return any contract-blocking problems."""
    node = contract_node(extra)
    if node is None:
        raise ValueError(f"{extra!r} is not a bridge-contract extra")

    problems: list[str] = []
    if checkout_sha != expected_sha:
        problems.append(
            f"checkout SHA {checkout_sha!r} does not match candidate SHA {expected_sha!r}"
        )

    try:
        counts = junit_counts(junit_path)
    except (OSError, ET.ParseError, ValueError) as exc:
        counts = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
        problems.append(f"cannot read JUnit evidence: {exc}")
    else:
        if counts["tests"] == 0:
            problems.append("the exact bridge contract collected zero tests")
        for name in ("failures", "errors", "skipped"):
            if counts[name]:
                problems.append(f"the exact bridge contract reported {counts[name]} {name}")

    evidence = {
        "schema_version": 1,
        "status": "passed" if not problems else "failed",
        "extra": extra,
        "contract_node": node,
        "candidate_sha": expected_sha,
        "checkout_sha": checkout_sha,
        "exact_candidate_checkout": checkout_sha == expected_sha,
        "junit": counts,
    }
    return evidence, problems


def _checkout_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_report(args: argparse.Namespace) -> int:
    evidence, problems = build_evidence(
        extra=args.extra,
        expected_sha=args.expected_sha,
        checkout_sha=_checkout_sha(),
        junit_path=args.junit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if problems:
        for problem in problems:
            print(f"bridge extras evidence: FAIL: {problem}")
        return 1
    print(
        "bridge extras evidence: PASS: "
        f"{args.extra} at {evidence['checkout_sha']} ({evidence['junit']['tests']} tests)"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    node_parser = subparsers.add_parser("node", help="Emit the GitHub output for one extra.")
    node_parser.add_argument("extra")

    report_parser = subparsers.add_parser("report", help="Write exact-SHA JUnit evidence.")
    report_parser.add_argument("--extra", required=True)
    report_parser.add_argument("--expected-sha", required=True)
    report_parser.add_argument("--junit", required=True, type=Path)
    report_parser.add_argument("--output", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "node":
        print(f"node={contract_node(args.extra) or ''}")
        return 0
    return _write_report(args)


if __name__ == "__main__":
    raise SystemExit(main())
