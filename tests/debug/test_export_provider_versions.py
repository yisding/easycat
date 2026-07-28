from types import SimpleNamespace

from easycat.debug.export import _collect_provider_versions


def test_bundle_manifest_includes_agent_version_info() -> None:
    agent = SimpleNamespace(
        version_info=lambda: {
            "provider": "openai_agents",
            "model": "gpt-test",
            "sdk_version": "1.2.3",
        }
    )

    assert _collect_provider_versions(SimpleNamespace(agent=agent))["agent"] == {
        "provider": "openai_agents",
        "model": "gpt-test",
        "sdk_version": "1.2.3",
    }
