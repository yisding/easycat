"""Local LlamaAgents bridge tests."""

from __future__ import annotations

from easycat.integrations.agents._helpers import INTERRUPTION_NOTE

from ._llama_agents_bridge_support import (
    AgentTurnInput,
    Any,
    BridgeInputError,
    CancellationMode,
    CancelToken,
    InMemoryArtifactStore,
    InMemoryRingBuffer,
    JournalAgentRecorder,
    LlamaAgentsBridge,
    RecorderContext,
    _BlockingWorkflow,
    _CancelTrackingHitlWorkflow,
    _HitlWorkflow,
    _LocalWorkflow,
    _recorder,
    _SecretContext,
    _StopEvent,
    _TextEvent,
    asyncio,
    gc,
    pytest,
)


class TestLocalLlamaAgentsBridge:
    @pytest.mark.asyncio
    async def test_streams_workflow_events_and_records_cursor(self, fake_workflows_modules):
        workflow = _LocalWorkflow(
            events=[_TextEvent("Hel"), _TextEvent("lo"), _StopEvent("Hello")]
        )
        bridge = LlamaAgentsBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal)):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == ["Hel", "lo"]
        assert [event.text for event in events if event.kind == "done"] == ["Hello"]
        assert workflow.calls[0]["message"] == "hi"
        assert "unit_entered" in [record.name for record in journal.read()]
        assert "unit_exited" in [record.name for record in journal.read()]

    @pytest.mark.asyncio
    async def test_uses_final_result_when_stream_has_no_text(self, fake_workflows_modules):
        workflow = _LocalWorkflow(events=[_StopEvent("ignored")], result={"result": "Final text"})
        bridge = LlamaAgentsBridge(workflow=workflow)

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            events.append(event)

        assert [event.text for event in events if event.kind == "text_delta"] == ["Final text"]
        assert [event.text for event in events if event.kind == "done"] == ["Final text"]

    @pytest.mark.asyncio
    async def test_preserves_context_between_runs(self, fake_workflows_modules):
        workflow = _LocalWorkflow(result="ok")
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("one"), _recorder()):
            pass
        first_ctx = workflow.last_handler.ctx
        async for _ in bridge.invoke(AgentTurnInput.from_text("two"), _recorder()):
            pass

        assert workflow.calls[1]["ctx"] is first_ctx

    @pytest.mark.asyncio
    async def test_cancellation_calls_handler_cancel_run(self, fake_workflows_modules):
        workflow = _LocalWorkflow(events=[_TextEvent("late")], result="late")
        bridge = LlamaAgentsBridge(workflow=workflow)
        token = CancelToken()
        token.cancel()

        events = []
        async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
            events.append(event)

        assert workflow.last_handler.cancelled is True
        assert [event.text for event in events if event.kind == "text_delta"] == []

    @pytest.mark.asyncio
    async def test_cancellation_during_idle_stream_is_prompt(self, fake_workflows_modules):
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)
        token = CancelToken()
        collected: list[Any] = []

        async def _consume() -> None:
            async for event in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(), token):
                collected.append(event)

        async def _cancel_when_blocked() -> None:
            await handler.streaming_blocked.wait()
            token.cancel()

        # Without racing the stream wait against cancellation this would hang
        # on the blocked step until the timeout fires.
        await asyncio.wait_for(asyncio.gather(_consume(), _cancel_when_blocked()), timeout=2.0)

        assert handler.cancelled is True
        assert [e.text for e in collected if e.kind == "text_delta"] == ["partial "]

    @pytest.mark.asyncio
    async def test_hard_task_cancel_preserves_terminal_handler_ctx(self, fake_workflows_modules):
        """Cancelling the invoke task (not just the token) must still stop the
        workflow; a handler that then confirms completion can preserve Context."""
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async def _consume() -> None:
            async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
                pass

        task = asyncio.ensure_future(_consume())
        await asyncio.wait_for(handler.streaming_blocked.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["has_context"] is True

    @pytest.mark.asyncio
    async def test_early_generator_close_cancels_running_workflow(self, fake_workflows_modules):
        """Closing invoke() mid-stream (GeneratorExit) must stop the workflow.

        A text-session interruption aclose()s the invoke generator instead of
        setting the cancel_token; without propagating that close into the
        inner stream the workflow keeps running and contaminates later turns.
        """
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
        first = await agen.__anext__()
        assert first.kind == "text_delta" and first.text == "partial "

        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["has_context"] is True

    @pytest.mark.asyncio
    async def test_early_generator_close_tolerates_cancelled_handler_cleanup(
        self, fake_workflows_modules
    ):
        """A handler's own cancellation must not turn a normal close into an error."""
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async def _cancel_run() -> None:
            handler.cancelled = True
            handler._never.set()
            raise asyncio.CancelledError

        handler.cancel_run = _cancel_run
        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder())
        first = await agen.__anext__()
        assert first.kind == "text_delta" and first.text == "partial "

        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert handler.cancelled is True

    @pytest.mark.asyncio
    async def test_nonterminal_interruption_drops_ctx_and_records_loss(
        self, fake_workflows_modules
    ):
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        # Simulate cancel_run() returning on its timeout while the workflow
        # runtime still considers this handler active.
        handler.is_done = lambda: False
        bridge = LlamaAgentsBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)

        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal))
        first = await agen.__anext__()
        assert first.kind == "text_delta"
        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["has_context"] is False
        errors = [record for record in journal.read() if record.name == "framework_error"]
        assert errors
        assert errors[-1].error is not None
        assert errors[-1].error.type == "LlamaWorkflowContextDropped"

    @pytest.mark.asyncio
    async def test_nonterminal_interruption_without_preservation_is_not_an_error(
        self, fake_workflows_modules
    ):
        workflow = _BlockingWorkflow()
        handler = workflow.handler
        handler.is_done = lambda: False
        bridge = LlamaAgentsBridge(workflow=workflow, preserve_context=False)
        journal = InMemoryRingBuffer(capacity=1000)

        agen = bridge.invoke(AgentTurnInput.from_text("hi"), _recorder(journal))
        first = await agen.__anext__()
        assert first.kind == "text_delta"
        await asyncio.wait_for(agen.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["has_context"] is False
        assert not [record for record in journal.read() if record.name == "framework_error"]

    @pytest.mark.asyncio
    async def test_reset_cancels_paused_local_hitl_handler(self, fake_workflows_modules):
        """reset() after a HITL pause must cancel the paused handler/stream,
        not just drop the references and leak the waiting workflow."""
        workflow = _CancelTrackingHitlWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        bridge.reset()
        cleanup_tasks = bridge._reset_cleanup_task_scope.tasks()
        assert {task.get_name() for task in cleanup_tasks} == {
            "easycat-llama-reset-local-handler",
            "easycat-llama-reset-local-stream",
        }
        await asyncio.gather(*cleanup_tasks)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["waiting_for_input"] is False

    @pytest.mark.asyncio
    async def test_aclose_cancels_paused_local_hitl_handler(self, fake_workflows_modules):
        """Session.stop()/shutdown() call aclose_if_supported(self.agent), not
        reset(), so aclose() must tear down a HITL-paused local handler too --
        and await it so the workflow is released before teardown returns."""
        workflow = _CancelTrackingHitlWorkflow()
        handler = workflow.handler
        bridge = LlamaAgentsBridge(workflow=workflow)

        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        await asyncio.wait_for(bridge.aclose(), timeout=2.0)

        assert handler.cancelled is True
        assert bridge.snapshot_state().fields["waiting_for_input"] is False

    @pytest.mark.asyncio
    async def test_human_input_event_pauses_and_resumes_handler(self, fake_workflows_modules):
        workflow = _HitlWorkflow()
        bridge = LlamaAgentsBridge(workflow=workflow)

        first_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("start"), _recorder()):
            first_turn.append(event)

        assert [event.text for event in first_turn if event.kind == "text_delta"] == [
            "What is your name?"
        ]
        assert [event.text for event in first_turn if event.kind == "done"] == [
            "What is your name?"
        ]

        second_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder()):
            second_turn.append(event)

        assert workflow.handler.sent_events[-1].response == "Ada"
        assert [event.text for event in second_turn if event.kind == "text_delta"] == [
            "Thanks ",
            "Ada",
        ]
        assert [event.text for event in second_turn if event.kind == "done"] == ["Thanks Ada"]
        # The pause resumed the original live stream cursor instead of
        # opening a second stream that would replay the prompt forever.
        assert workflow.handler.stream_calls == 1

    @pytest.mark.asyncio
    async def test_paused_local_stream_survives_wrapper_finalization(self, fake_workflows_modules):
        """A HITL pause must keep the saved live stream open even after the
        abandoned cancellation wrapper is finalized.

        On the normal Session path invoke() runs with a cancel_token and many
        awaits elapse between user turns, so the abandoned
        _aiter_with_cancellation wrapper is garbage-collected and its
        finalizer aclose()s its source. Before the fix that closed the very
        stream saved as _pending_local_stream, so the resumed turn streamed
        nothing from the post-response cursor and the workflow's answer was
        lost.
        """
        workflow = _HitlWorkflow()
        bridge = LlamaAgentsBridge(workflow=workflow)

        token1 = CancelToken()
        async for _ in bridge.invoke(AgentTurnInput.from_text("start"), _recorder(), token1):
            pass
        assert bridge.snapshot_state().fields["waiting_for_input"] is True

        # Mirror the real Session path: the event loop keeps running between
        # user turns, so the abandoned wrapper is finalized before resume.
        for _ in range(10):
            gc.collect()
            await asyncio.sleep(0)

        token2 = CancelToken()
        second_turn = []
        async for event in bridge.invoke(AgentTurnInput.from_text("Ada"), _recorder(), token2):
            second_turn.append(event)

        assert workflow.handler.sent_events[-1].response == "Ada"
        assert [e.text for e in second_turn if e.kind == "text_delta"] == [
            "Thanks ",
            "Ada",
        ]
        assert [e.text for e in second_turn if e.kind == "done"] == ["Thanks Ada"]
        # Resumed the same live cursor rather than re-streaming the prompt.
        assert workflow.handler.stream_calls == 1

    def test_apply_interruption_uses_atomic_recorder_and_delegate(self, fake_workflows_modules):
        workflow = _LocalWorkflow(result="ok")
        bridge = LlamaAgentsBridge(workflow=workflow)
        journal = InMemoryRingBuffer(capacity=1000)

        bridge.apply_interruption("part", CancellationMode.IMMEDIATE_STOP, _recorder(journal))

        assert workflow.interruption == ("part", CancellationMode.IMMEDIATE_STOP)
        assert bridge._pending_interruption_note == INTERRUPTION_NOTE
        names = [record.name for record in journal.read()]
        assert "state_committed" in names
        assert "cancellation_boundary" in names

    @pytest.mark.asyncio
    async def test_default_truncate_note_reaches_next_start_event(self, fake_workflows_modules):
        workflow = _LocalWorkflow(result="ok")
        bridge = LlamaAgentsBridge(workflow=workflow)

        bridge.apply_interruption("part", CancellationMode.IMMEDIATE_STOP, _recorder())
        async for _ in bridge.invoke(AgentTurnInput.from_text("next"), _recorder()):
            pass

        assert workflow.calls[0]["easycat_interruption_note"] == INTERRUPTION_NOTE
        assert bridge._pending_interruption_note is None

    @pytest.mark.asyncio
    async def test_start_failure_keeps_interruption_note_for_local_retry(
        self, fake_workflows_modules
    ):
        class _FailOnceWorkflow(_LocalWorkflow):
            def __init__(self) -> None:
                super().__init__(result="ok")
                self._fail_once = True

            def run(self, **kwargs: Any):
                if self._fail_once:
                    self._fail_once = False
                    self.calls.append(kwargs)
                    raise RuntimeError("workflow start failed")
                return super().run(**kwargs)

        workflow = _FailOnceWorkflow()
        bridge = LlamaAgentsBridge(workflow=workflow)
        bridge.append_interruption_note("retry-note")

        with pytest.raises(RuntimeError, match="workflow start failed"):
            async for _ in bridge.invoke(AgentTurnInput.from_text("next"), _recorder()):
                pass

        assert workflow.calls[0]["easycat_interruption_note"] == "retry-note"
        assert bridge._pending_interruption_note == "retry-note"

        async for _ in bridge.invoke(AgentTurnInput.from_text("next"), _recorder()):
            pass

        assert workflow.calls[1]["easycat_interruption_note"] == "retry-note"
        assert bridge._pending_interruption_note is None

    @pytest.mark.asyncio
    async def test_interruption_snapshot_scrubs_context_secrets(self, fake_workflows_modules):
        """An interrupted workflow's Context must not leak credentials into
        the debug-bundle artifact written by record_state_snapshot()."""
        workflow = _LocalWorkflow(events=[_TextEvent("hi"), _StopEvent("hi")])
        bridge = LlamaAgentsBridge(workflow=workflow)
        async for _ in bridge.invoke(AgentTurnInput.from_text("hi"), _recorder()):
            pass
        bridge._ctx = _SecretContext()

        journal = InMemoryRingBuffer(capacity=1000)
        store = InMemoryArtifactStore()
        recorder = JournalAgentRecorder(
            journal=journal,
            artifact_store=store,
            context=RecorderContext(run_id="r1", session_id="s1", turn_id="t1"),
        )

        bridge.apply_interruption("part", CancellationMode.IMMEDIATE_STOP, recorder)

        refs = [
            r.output_ref for r in journal.read() if r.name == "state_snapshot" and r.output_ref
        ]
        assert refs, "expected a state snapshot artifact to be written"
        blob = b"".join(store.get(ref) or b"" for ref in refs).decode()
        assert "api_key" not in blob
        assert "sk-super-secret-value" not in blob
        assert "auth_token" not in blob
        assert "bearer-leak-me" not in blob
        # Non-secret context state is preserved for debugging.
        assert "ada-lovelace" in blob
        assert "non-secret-ok" in blob

    def test_constructor_requires_single_mode(self):
        with pytest.raises(BridgeInputError, match="requires"):
            LlamaAgentsBridge()
        with pytest.raises(BridgeInputError, match="not both"):
            LlamaAgentsBridge(workflow=object(), client=object(), workflow_name="wf")
        with pytest.raises(BridgeInputError, match="workflow_name"):
            LlamaAgentsBridge(client=object())
