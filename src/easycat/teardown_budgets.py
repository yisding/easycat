"""Canonical defaults for bounded lifecycle cleanup.

Only concrete lifecycle-policy defaults belong here. Provider and transport
protocol bounds stay with their protocols, while publicly configurable
lifecycle fields import their default values from this module.
"""

from __future__ import annotations

from typing import Final

# Let an agent generator run its immediate post-``done`` cleanup without
# permitting a broken stream to hold turn finalization indefinitely.
AGENT_POST_DONE_STREAM_DRAIN_TIMEOUT_S: Final = 0.01

# A Llama WorkflowHandler has already received its normal cancellation request;
# this is the final safety net for a non-cooperative workflow step.
LLAMA_POST_CANCEL_AWAIT_TIMEOUT_S: Final = 2.0

# Give a terminal Remote Responses SSE stream a brief opportunity to reach EOF
# and close its nested line reader.
REMOTE_RESPONSES_COMPLETED_STREAM_DRAIN_TIMEOUT_S: Final = 0.05
