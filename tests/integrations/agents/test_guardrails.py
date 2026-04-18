"""AC2.10, AC2.15: Guardrail tests — no tool registry, no realtime bridge."""

from __future__ import annotations

import subprocess


class TestNoToolRegistry:
    """AC2.10 — no EasyCat-native tool abstraction, registry, or decorator."""

    def test_no_easycat_native_tool_code(self):
        patterns = [
            "@easycat_tool",
            "@register_tool",
            "@register_function",
            "class ToolRegistry",
            "class MCPClient",
            "def register_tool",
        ]
        for pattern in patterns:
            result = subprocess.run(
                ["grep", "-r", pattern, "src/easycat/"],
                capture_output=True,
                text=True,
            )
            assert result.stdout.strip() == "", (
                f"Found tool registry pattern {pattern!r} in src/easycat/:\n{result.stdout}"
            )


class TestNoRealtimeBridgeSurface:
    """AC2.15 — no realtime bridge surface in bridge/stage/session layers."""

    def test_no_realtime_bridge_references(self):
        patterns = ["RealtimeBridge", "realtime_session", "RealtimeStage"]
        search_dirs = [
            "src/easycat/integrations/",
            "src/easycat/session/",
            "src/easycat/runtime/",
        ]
        for pattern in patterns:
            for search_dir in search_dirs:
                result = subprocess.run(
                    ["grep", "-r", pattern, search_dir],
                    capture_output=True,
                    text=True,
                )
                assert result.stdout.strip() == "", (
                    f"Found realtime pattern {pattern!r} in {search_dir}:\n{result.stdout}"
                )
