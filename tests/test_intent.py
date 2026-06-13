"""Tests for reusable voice intent-classification helpers."""

from __future__ import annotations

import pytest

from easycat.intent import (
    IntentClassification,
    IntentSpec,
    classify_intent_with_optional_fallback,
)


class _FakeGenericClassifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, IntentSpec]] = []

    async def classify_intent(self, text: str, spec: IntentSpec) -> IntentClassification:
        self.calls.append((text, spec))
        return IntentClassification(matched=True, evidence="semantic match", method="llm")


@pytest.mark.asyncio
async def test_classify_intent_short_circuits_on_rule_match() -> None:
    spec = IntentSpec(name="escalation", description="user asks for a human")
    classifier = _FakeGenericClassifier()

    result = await classify_intent_with_optional_fallback(
        "representative please",
        spec=spec,
        rule_matcher=lambda _text: "representative",
        classifier=classifier,
    )

    assert result.matched
    assert result.method == "rule"
    assert result.evidence == "representative"
    assert classifier.calls == []


@pytest.mark.asyncio
async def test_classify_intent_uses_generic_classifier_after_rule_miss() -> None:
    spec = IntentSpec(name="escalation", description="user asks for a human")
    classifier = _FakeGenericClassifier()

    result = await classify_intent_with_optional_fallback(
        "can someone real help me?",
        spec=spec,
        rule_matcher=lambda _text: None,
        classifier=classifier,
    )

    assert result.matched
    assert result.method == "llm"
    assert result.evidence == "semantic match"
    assert classifier.calls == [("can someone real help me?", spec)]
