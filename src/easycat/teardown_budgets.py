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

# Wait for queued audio and the active transport write before turn teardown.
SESSION_AUDIO_DRAIN_TIMEOUT_S: Final = 2.0

# Extend local-speaker drain by a small scheduling/buffer margin.
SESSION_AUDIO_PLAYOUT_MARGIN_S: Final = 0.5

# Once the caller is cancelled, bound the protected first-frame transport send.
SESSION_INLINE_SEND_TIMEOUT_S: Final = 0.5

# Reap or terminate a cancelled inline send in short bounded stages.
SESSION_INLINE_SEND_CANCEL_GRACE_TIMEOUT_S: Final = 0.1

# A forced stop must not remain behind startup code that ignores cancellation.
SESSION_FORCE_START_LOCK_TIMEOUT_S: Final = 0.5

# Give a superseded graceful stop a bounded opportunity to unwind.
SESSION_SUPERSEDED_STOP_TIMEOUT_S: Final = 0.5

# Bound transport audio cutoff during barge-in cleanup.
SESSION_BARGE_IN_CUTOFF_TIMEOUT_S: Final = 0.4

# Briefly drain a superseded application prompt before abandoning its cleanup.
SESSION_APPLICATION_PROMPT_CANCEL_DRAIN_TIMEOUT_S: Final = 0.1
