from __future__ import annotations


def test_teaching_example_compatibility_imports() -> None:
    from easycat import CoreSessionActionExecutor, MarkdownStripProcessor
    from easycat.session import split_at_sentence_boundaries

    assert CoreSessionActionExecutor.__name__ == "CoreSessionActionExecutor"
    assert MarkdownStripProcessor.__name__ == "MarkdownStripProcessor"
    assert split_at_sentence_boundaries("Hello world. ") == ("Hello world. ", "")
