"""OpenAI Agents SDK bridge for the debug-first runtime.

Implements :class:`ExternalAgentBridge` on top of the ``agents`` package
and records execution state to the journal via :class:`AgentRecorder`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from easycat.cancel import CancelToken
from easycat.integrations.agents._context import normalize_context_messages
from easycat.integrations.agents._helpers import (
    aclose_quietly,
    bridge_version_info,
    record_usage_from_result,
    split_replacement_by_original_parts,
)
from easycat.integrations.agents._openai_agents_events import (
    extract_text_delta,
    extract_tool_delta,
    map_run_item,
)
from easycat.integrations.agents._state_serialization import serialize_framework_state
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentRecorder,
    AgentTurnInput,
    CancellationMode,
    CommitRule,
    ExecutionCursor,
    FrameworkStateSnapshot,
    InterruptionPlan,
    UnitKind,
    apply_standard_interruption,
)
from easycat.runtime.records import ErrorInfo

# Optional dependency: under type-checking mypy sees the real ``Runner`` type,
# while at runtime it may be absent.  This keeps the gated type-check stable
# regardless of whether openai-agents is installed in the check env (the
# ``except`` branch is not analysed, so ``Runner = None`` never conflicts with
# the imported type).
if TYPE_CHECKING:
    from agents import Runner
else:
    try:
        from agents import Runner
    except ImportError:
        Runner = None

logger = logging.getLogger(__name__)

_OPENAI_AGENTS_WARMUP_TIMEOUT_SECONDS = 2.0


def _drop_dangling_function_calls(history: list[Any]) -> list[Any]:
    """Remove ``function_call`` items whose output never arrived.

    A hard-cancelled run (barge-in ``aclose()``) can snapshot
    ``to_input_list()`` mid-tool-call; the Responses API rejects an input
    list containing a ``function_call`` with no matching
    ``function_call_output``, so replaying such a snapshot would fail every
    later turn. Non-dict items and everything else pass through unchanged.
    """
    output_ids = {
        item.get("call_id")
        for item in history
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    }
    return [
        item
        for item in history
        if not (
            isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("call_id") not in output_ids
        )
    ]


def _resolve_model_id(candidate: Any) -> str | None:
    """Best-effort string model id from a string or SDK ``Model`` candidate.

    Returns a stripped, non-empty model id, or ``None`` when the candidate is
    missing or carries no usable string id.  SDK ``Model`` objects expose the
    id on a ``.model`` attribute.
    """
    if candidate is None:
        return None
    if isinstance(candidate, str):
        stripped = candidate.strip()
        return stripped or None
    model_attr = getattr(candidate, "model", None)
    if isinstance(model_attr, str) and model_attr.strip():
        return model_attr.strip()
    return None


def _history_role(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("role")
    return getattr(item, "role", None)


def _replace_assistant_content(item: Any, replacement: str) -> str | None:
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, list):
            parts = [
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "output_text"
            ]
            if not parts:
                return None
            originals = [str(part.get("text", "")) for part in parts]
            replacements = split_replacement_by_original_parts(originals, replacement)
            for part, text in zip(parts, replacements):
                part["text"] = text
            return "".join(originals)
        if isinstance(content, str):
            item["content"] = replacement
            return content
        return None
    if hasattr(item, "content"):
        original = getattr(item, "content", None)
        item.content = replacement
        return original
    return None


class OpenAIAgentsBridge:
    """Bridge wrapping an OpenAI Agents SDK ``Agent``.

    Implements ``ExternalAgentBridge`` while capturing agent transitions,
    tool calls, and handoffs to the journal via the ``AgentRecorder``.
    """

    COMMITTABLE_BOUNDARIES: ClassVar[Mapping[UnitKind | str, CommitRule]] = {
        UnitKind.TOOL_CALL: CommitRule.BETWEEN_PHASES,
        UnitKind.MODEL_NODE: CommitRule.NON_COMMITTABLE,
        UnitKind.AGENT: CommitRule.BETWEEN_TURNS,
    }

    def __init__(
        self,
        agent: Any,
        *,
        run_config: Any = None,
        context: Any = None,
        use_previous_response_id: bool = True,
        max_turns: int | None = None,
        hooks: Any = None,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        self._agent = agent
        self._original_agent = agent
        self._run_config = run_config
        self._context = context
        self._use_previous_response_id = use_previous_response_id
        self._max_turns = max_turns
        self._hooks = hooks
        self._mcp_servers = mcp_servers
        self._previous_response_id: str | None = None
        self._pending_interruption: str | None = None
        self._message_history: list[Any] = []
        self._last_output: Any = None

    async def warmup(self) -> None:
        """Prime DNS/TLS/connection-pool via the SDK's shared OpenAI client.

        Issues a cheap, unbilled single-model metadata request through the
        same ``AsyncOpenAI`` client the ``Runner`` reuses, so the first real
        turn does not pay the cold-connection cost. This deliberately avoids a
        billed ``Runner.run`` and bounds the request so live WebRTC offer
        handling is not held hostage by a slow warmup. All failures are
        swallowed — ``WarmupRunner`` re-raises, so an auth/timeout error here
        must not abort ``Session.start()``.
        """
        try:
            from agents.models import default_models
            from agents.models.openai_provider import OpenAIProvider

            client = OpenAIProvider()._get_client()
            model_name = self._warmup_model_name(default_models.get_default_model())
            await client.models.retrieve(
                model_name,
                timeout=_OPENAI_AGENTS_WARMUP_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            logger.debug("OpenAI Agents warmup skipped: %s", exc)

    def _warmup_model_name(self, default_model: str) -> str:
        """Return the string model name that should be touched during warmup.

        ``run_config.model`` and ``agent.model`` may be a plain string *or* an
        SDK ``Model`` object (e.g. ``OpenAIResponsesModel``); for objects the
        model id lives on a ``.model`` attribute.  Resolving both keeps warmup
        priming the model the runtime actually uses instead of silently probing
        ``default_model`` whenever a config passes a ``Model`` instance.
        """
        for candidate in (
            getattr(self._run_config, "model", None),
            getattr(self._agent, "model", None),
        ):
            resolved = _resolve_model_id(candidate)
            if resolved is not None:
                return resolved
        return default_model

    # ── ExternalAgentBridge interface ─────────────────────────────

    def version_info(self) -> dict[str, str]:
        """Return model and SDK metadata for journal reproducibility."""
        return bridge_version_info(
            provider="openai_agents",
            model=self._model_name(),
            distribution="openai-agents",
        )

    def _model_name(self) -> str | None:
        for candidate in (
            getattr(self._run_config, "model", None),
            getattr(self._agent, "model", None),
        ):
            resolved = _resolve_model_id(candidate)
            if resolved is not None:
                return resolved
        return None

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: AgentRecorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        if Runner is None:
            raise ImportError(
                "openai-agents package is required: uv add 'easycat[openai-agents]'. "
                "From the EasyCat repo, use: uv sync --extra openai-agents --group dev."
            )

        agent_cursor = ExecutionCursor(
            unit_id=f"agent-{uuid4().hex[:8]}",
            unit_kind=UnitKind.AGENT,
            display_name=getattr(self._agent, "name", "OpenAIAgent"),
            entered_at=time.monotonic_ns(),
            committable=False,
        )
        # NOTE: this bridge deliberately does NOT wrap the turn in
        # ``recorder.turn_cursor(...)`` (the canonical enter → error →
        # BaseException → clean-exit ordering used by the other bridges).  Its
        # clean/cancelled cursor close lives inside the ``finally`` below so the
        # SDK-state snapshot (``to_input_list`` / ``last_response_id``) runs on
        # every path, and a handoff exit ENTERS a second cursor -- neither fits
        # a single-cursor context manager.  The arms below still follow
        # ``turn_cursor``'s semantics: setup/stream ``Exception`` →
        # framework_error + unit_exited(reason="error"); non-handoff clean/cancel
        # exit → committed exit(reason=None).
        recorder.record_unit_entered(agent_cursor)

        saved_mcp_servers = getattr(self._agent, "mcp_servers", None)
        try:
            pending_interruption = (
                self._pending_interruption
                if self._use_previous_response_id and self._previous_response_id is not None
                else None
            )
            input_data = self._build_input(turn_input)
            kwargs = self._build_kwargs()
            if self._mcp_servers is not None and hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = list(self._mcp_servers)
            result = Runner.run_streamed(self._agent, input_data, **kwargs)
            # ``run_streamed`` has accepted the input at this point.  Do not
            # consume a pending interruption while constructing it: a
            # synchronous SDK startup failure means the server never saw the
            # note, and a retry must include it again.
            if (
                pending_interruption is not None
                and self._pending_interruption == pending_interruption
            ):
                self._pending_interruption = None
        except Exception as exc:
            if hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = saved_mcp_servers
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            recorder.record_unit_exited(agent_cursor, reason="error")
            raise

        accumulated = ""
        pending_tool_calls: dict[str, str] = {}
        run_cancelled = False
        stream_failed = False
        usage_model_ambiguous = False
        event_stream: AsyncIterator[Any] | None = None

        try:
            event_stream = result.stream_events()
            async for event in event_stream:
                if cancel_token and cancel_token.is_cancelled:
                    if not run_cancelled:
                        run_cancelled = True
                        # ``Runner.run_streamed`` drives the agent loop in a
                        # background task; abandoning ``stream_events()`` does
                        # not stop it -- on GC-finalize the SDK awaits the run
                        # to completion, firing post-cancel tool side-effects,
                        # billing tokens, and snapshotting ``to_input_list()``/
                        # ``last_response_id`` from a still-mutating run.
                        # Explicitly cancel: ``after_turn`` drains in-flight
                        # tools, ``immediate`` stops now.  Keep consuming
                        # ``stream_events()`` afterwards so the cancellation
                        # settles, as the SDK requires.
                        result.cancel(mode="after_turn" if pending_tool_calls else "immediate")
                    if pending_tool_calls:
                        if event.type == "run_item_stream_event":
                            item_type = getattr(event.item, "type", "")
                            usage_model_ambiguous |= item_type in {
                                "handoff_call_item",
                                "handoff_output_item",
                                "tool_call_item",
                            }
                            bridge_ev = map_run_item(event.item, recorder, pending_tool_calls)
                            if bridge_ev is not None:
                                yield bridge_ev
                        elif event.type == "raw_response_event":
                            bridge_ev = extract_tool_delta(event.data)
                            if bridge_ev is not None:
                                yield bridge_ev
                        continue
                    else:
                        # No tools in flight (or the last pending tool just
                        # drained): the cancel set ``is_complete``, so drain to
                        # the natural end of the stream rather than abandoning
                        # the generator -- ``after_turn`` keeps emitting events
                        # while the SDK saves session state, and snapshotting
                        # ``to_input_list()``/``last_response_id`` before that
                        # settles would capture a still-mutating run.
                        continue

                if event.type == "raw_response_event":
                    delta = extract_text_delta(event.data)
                    if delta:
                        accumulated += delta
                        yield AgentBridgeEvent(kind="text_delta", text=delta)
                    else:
                        bridge_ev = extract_tool_delta(event.data)
                        if bridge_ev is not None:
                            yield bridge_ev
                elif event.type == "run_item_stream_event":
                    item_type = getattr(event.item, "type", "")
                    usage_model_ambiguous |= item_type in {
                        "handoff_call_item",
                        "handoff_output_item",
                        "tool_call_item",
                    }
                    bridge_ev = map_run_item(event.item, recorder, pending_tool_calls)
                    if bridge_ev is not None:
                        yield bridge_ev
        except (GeneratorExit, asyncio.CancelledError):
            # An external ``aclose()`` of this generator (a text-session
            # barge-in closes ``invoke()``) or a hard task cancel tears the
            # turn down without the cooperative ``cancel_token``.  Neither is
            # an ``Exception``, so the arm below is skipped and the ``finally``
            # would snapshot a still-running background run.  Explicitly cancel
            # it (mirroring the Llama bridge); a cancelled turn is not a
            # framework fault, so do not record a framework error.  Guard the
            # cancel itself: an SDK error here would supersede the in-flight
            # ``GeneratorExit`` and turn a clean close into
            # ``RuntimeError("async generator ignored GeneratorExit")``.
            try:
                result.cancel(mode="immediate")
            except Exception:
                logger.debug("RunResultStreaming.cancel() raised during close", exc_info=True)
            raise
        except Exception as exc:
            stream_failed = True
            recorder.record_framework_error(ErrorInfo.from_exception(exc))
            raise
        finally:
            # ``result.stream_events()`` may own an SDK event generator. An
            # outer consumer close only injects ``GeneratorExit`` into this
            # bridge, not into that delegated iterator, so release it before
            # snapshotting the now-settled SDK state.
            await aclose_quietly(event_stream)
            # This ``finally`` also runs on ``GeneratorExit`` /
            # ``CancelledError`` injected by ``AgentRunner``'s timeout/
            # barge-in ``aclose()`` (neither is an ``Exception`` so the
            # block above is skipped).  ``result.to_input_list()`` can
            # raise when the stream was cancelled mid-flight; guard it so
            # the agent cursor below is *always* closed, otherwise it is
            # left without a ``unit_exited`` record and breaks the
            # recorder's strict stack invariant for the postmortem journal.
            if hasattr(self._agent, "mcp_servers"):
                self._agent.mcp_servers = saved_mcp_servers
            try:
                history = result.to_input_list()
            except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                pass
            else:
                # A hard ``aclose()`` cancel can snapshot before the run
                # settles, capturing a ``function_call`` item whose
                # ``function_call_output`` never arrived; replaying that
                # history is rejected by the Responses API. Drop the
                # unmatched calls rather than poisoning every later turn.
                self._message_history = _drop_dangling_function_calls(history)
            if self._use_previous_response_id:
                self._previous_response_id = getattr(result, "last_response_id", None)
            last_agent = getattr(result, "last_agent", None)
            handed_off = last_agent is not None and last_agent is not self._agent
            await record_usage_from_result(
                recorder,
                result,
                provider="openai_agents",
                # The SDK reports aggregate run usage. Once a handoff occurs,
                # attributing that total to either agent's model is misleading.
                model=None if handed_off or usage_model_ambiguous else self._model_name(),
            )
            if stream_failed:
                recorder.safe_exit_cursor(agent_cursor, reason="error")
            elif handed_off:
                # Record handoff.
                old_name = getattr(self._agent, "name", "unknown")
                new_name = getattr(last_agent, "name", "unknown")
                recorder.record_unit_exited(agent_cursor.with_committable(True), reason="handoff")
                recorder.record_framework_handoff(
                    from_unit=old_name,
                    to_unit=new_name,
                    reason="agent_handoff",
                )
                self._agent = last_agent
                # Enter new agent cursor for the handoff target.
                new_cursor = ExecutionCursor(
                    unit_id=f"agent-{uuid4().hex[:8]}",
                    unit_kind=UnitKind.AGENT,
                    display_name=new_name,
                    entered_at=time.monotonic_ns(),
                    committable=True,
                )
                recorder.record_unit_entered(new_cursor)
                recorder.record_unit_exited(new_cursor.with_committable(True), reason=None)
            else:
                recorder.safe_exit_cursor(agent_cursor.with_committable(True), reason=None)

        self._last_output = getattr(result, "final_output", None)
        yield AgentBridgeEvent(
            kind="done",
            text=accumulated,
            structured_output=self._last_output,
        )

    def snapshot_state(self) -> FrameworkStateSnapshot:
        return FrameworkStateSnapshot(
            fields={
                "agent": getattr(self._agent, "name", "unknown"),
                "previous_response_id": self._previous_response_id,
                "turn_count": len(self._message_history),
            },
            kind="openai_agents",
        )

    def apply_interruption(
        self,
        delivered_text: str,
        mode: CancellationMode,
        recorder: AgentRecorder | None = None,
        caused_by_signal_id: str | None = None,
    ) -> None:
        apply_standard_interruption(self, delivered_text, mode, recorder, caused_by_signal_id)

    def _serialize_framework_state(self) -> bytes:
        """Serialize message history for artifact storage."""
        return serialize_framework_state(self._message_history, fallback=b"[]")

    def _plan_interruption(self, delivered_text: str, mode: CancellationMode) -> InterruptionPlan:
        replacement = delivered_text + "..." if delivered_text else ""
        pre_ref = f"openai-pre-{id(self._message_history):x}"
        post_ref = f"openai-post-{id(self._message_history):x}"
        return InterruptionPlan(
            mutation_kind="interrupt_truncate",
            pre_state_ref=pre_ref,
            post_state_ref=post_ref,
            framework_instructions={"replacement": replacement},
        )

    def _apply_planned_mutation(self, plan: InterruptionPlan) -> None:
        replacement = plan.framework_instructions["replacement"]
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = _history_role(item)
            if role == "user":
                # The latest user entry starts the current turn. Reaching it
                # without an assistant reply means this turn produced no
                # rewritable output, so the prior turn must remain intact.
                break
            if role == "assistant":
                _replace_assistant_content(item, replacement)
                break
        if self._use_previous_response_id and self._previous_response_id is not None:
            self._pending_interruption = (
                "[The user interrupted the assistant's response. "
                f'They approximately heard: "{replacement}"]'
            )

    def reset(self) -> None:
        self._agent = self._original_agent
        self._message_history.clear()
        self._previous_response_id = None
        self._pending_interruption = None
        self._last_output = None

    def configure_runtime(
        self,
        *,
        mcp_servers: list[str] | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Apply session-level MCP servers (model/api_key are unused here)."""
        if mcp_servers is not None:
            self._mcp_servers = list(mcp_servers)

    # ── History post-processing ───────────────────────────────────

    def replace_last_assistant_text(self, text: str) -> None:
        """Rewrite the last assistant entry in ``_message_history`` to ``text``.

        Called by the adapter shim after post-processing (e.g. Markdown
        stripping) so that subsequent turns condition on the cleaned text
        rather than the raw LLM output.
        """
        original: str | None = None
        for i in range(len(self._message_history) - 1, -1, -1):
            item = self._message_history[i]
            role = _history_role(item)
            if role == "user":
                # Do not let post-processing for an empty current turn reach
                # behind its user boundary and edit a prior assistant reply.
                break
            if role != "assistant":
                continue
            original = _replace_assistant_content(item, text)
            break

        # When chaining by response_id the server maintains its own
        # conversation and won't see local history edits.  Do not place
        # the rewritten assistant text in a developer note: the text is
        # model/user-influenced and would be promoted to higher-priority
        # instructions on the next turn.  Instead, break the response-id
        # chain so the next turn sends the locally corrected history.
        if (
            self._use_previous_response_id
            and self._previous_response_id is not None
            and original is not None
            and original != text
        ):
            self._previous_response_id = None
            self._pending_interruption = None

    def append_interruption_note(self, note: str) -> None:
        """Append an interruption note so the next turn sees it.

        Appends a ``developer``-role message to ``_message_history`` for
        the full-history code path, and also stores it as
        ``_pending_interruption`` so that the response-id chaining path
        in ``_build_input`` surfaces it on the next turn.
        """
        self._message_history.append({"role": "developer", "content": note})
        if self._use_previous_response_id:
            self._pending_interruption = note

    # ── Internal helpers ─────────────────────────────────────────

    def _build_input(self, turn_input: AgentTurnInput | str) -> Any:
        if isinstance(turn_input, AgentTurnInput):
            text = turn_input.text
            raw_context = turn_input.context
        else:
            text = str(turn_input)
            raw_context = []
        own_history = bool(self._message_history) or (
            self._use_previous_response_id and self._previous_response_id is not None
        )
        context = normalize_context_messages(raw_context, own_history=own_history)
        user_message = {"role": "user", "content": text}
        if self._use_previous_response_id and self._previous_response_id is not None:
            parts: list[dict[str, str]] = []
            parts.extend(context)
            if self._pending_interruption is not None:
                parts.append({"role": "developer", "content": self._pending_interruption})
            parts.append(user_message)
            return parts
        if context or self._message_history:
            return [*context, *self._message_history, user_message]
        return text

    def _build_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self._run_config is not None:
            kwargs["run_config"] = self._run_config
        if self._context is not None:
            kwargs["context"] = self._context
        if self._use_previous_response_id:
            if self._previous_response_id is not None:
                kwargs["previous_response_id"] = self._previous_response_id
            kwargs["auto_previous_response_id"] = True
        if self._max_turns is not None:
            kwargs["max_turns"] = self._max_turns
        if self._hooks is not None:
            kwargs["hooks"] = self._hooks
        return kwargs
