from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from threading import Thread

import pytest

from easycat._concurrency import RuntimeSupervisor, SurvivorCapacityError
from easycat.runtime.scope import (
    BackgroundTaskScope,
    JournalSink,
    RuntimeMemberKind,
    RuntimeMemberPolicy,
    RuntimeResultStatus,
    RuntimeScope,
    RuntimeScopeState,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)


def test_background_scope_closes_coroutine_when_task_creation_fails() -> None:
    scope = BackgroundTaskScope()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(RuntimeError, match="no running event loop"):
        scope.create_task("timer", coro)

    assert coro.cr_frame is None


def test_background_scope_closes_coroutine_for_empty_name() -> None:
    scope = BackgroundTaskScope()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(ValueError, match="must be non-empty"):
        scope.create_task("", coro)

    assert coro.cr_frame is None


@pytest.mark.asyncio
async def test_background_scope_closes_coroutine_for_duplicate_name() -> None:
    scope = BackgroundTaskScope()
    release = asyncio.Event()
    active = scope.create_task("timer", release.wait())

    async def rejected_work() -> None:
        pass

    rejected = rejected_work()
    with pytest.raises(RuntimeError, match="already active"):
        scope.create_task("timer", rejected)

    assert rejected.cr_frame is None
    release.set()
    await active


@pytest.mark.asyncio
async def test_background_scope_prunes_completed_task() -> None:
    scope = BackgroundTaskScope()

    task = scope.create_task("timer", asyncio.sleep(0))
    assert scope.active("timer")

    await task
    await asyncio.sleep(0)

    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_replaces_named_task() -> None:
    scope = BackgroundTaskScope()
    first_started = asyncio.Event()
    first_cleaned_up = asyncio.Event()
    release_second = asyncio.Event()

    async def first_timer() -> None:
        first_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            first_cleaned_up.set()

    first = scope.create_task("timer", first_timer())
    await first_started.wait()

    second = scope.create_task("timer", release_second.wait(), replace=True)
    await asyncio.sleep(0)

    assert first.cancelled()
    assert first_cleaned_up.is_set()
    assert scope.active("timer")
    assert scope.tasks() == (second,)

    release_second.set()
    await second
    await asyncio.sleep(0)

    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_cancel_detaches_name_immediately() -> None:
    scope = BackgroundTaskScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def timer() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = scope.create_task("timer", timer())
    await started.wait()

    assert scope.cancel("timer") == (task,)
    assert not scope.active("timer")
    assert scope.empty

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up.is_set()


@pytest.mark.asyncio
async def test_background_scope_cancel_detaches_current_task_without_cancelling_it() -> None:
    scope = BackgroundTaskScope()
    completed = asyncio.Event()

    async def cancel_owner() -> None:
        current = asyncio.current_task()
        assert current is not None
        assert scope.cancel("timer") == (current,)
        await asyncio.sleep(0)
        completed.set()

    task = scope.create_task("timer", cancel_owner())
    await task

    assert not task.cancelled()
    assert completed.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_background_scope_observes_task_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope = BackgroundTaskScope()

    async def fail() -> None:
        raise RuntimeError("boom")

    scope.create_task("timer", fail())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert scope.empty
    assert "Background task 'timer' failed" in caplog.text


@pytest.mark.asyncio
async def test_background_scope_retains_and_pops_typed_terminal_results() -> None:
    scope = BackgroundTaskScope(name="transport")

    async def succeed() -> str:
        return "closed"

    async def fail() -> None:
        raise RuntimeError("disconnect failed")

    success = scope.create_task("close", succeed(), retain_result=True)
    failure = scope.create_task("disconnect", fail(), retain_result=True)
    await asyncio.gather(success, failure, return_exceptions=True)
    await asyncio.sleep(0)

    results = scope.terminal_results()
    assert [result.name for result in results] == ["close", "disconnect"]
    assert results[0].owner_id == "transport"
    assert results[0].kind is RuntimeMemberKind.TASK
    assert results[0].status is RuntimeResultStatus.COMPLETED
    assert results[0].unwrap() == "closed"
    assert results[1].status is RuntimeResultStatus.RAISED
    with pytest.raises(RuntimeError, match="disconnect failed"):
        results[1].unwrap()

    assert scope.pop_terminal_results("disconnect") == (results[1],)
    assert scope.terminal_results() == (results[0],)
    assert scope.pop_terminal_results() == (results[0],)
    assert scope.terminal_results() == ()


@pytest.mark.asyncio
async def test_background_scope_default_mode_does_not_retain_result() -> None:
    scope = BackgroundTaskScope()

    await scope.create_task("timer", asyncio.sleep(0))
    await asyncio.sleep(0)

    assert scope.terminal_results() == ()


def _attached_root(
    name: str,
    *,
    supervisor: RuntimeSupervisor | None = None,
    survivor_capacity: int = 2,
) -> RuntimeScope:
    return RuntimeScope.create_root(
        name=name,
        root_id=f"test-root:{name}",
        supervisor=supervisor or RuntimeSupervisor(capacity=survivor_capacity),
        survivor_capacity=survivor_capacity,
    )


def _task_policy(
    *,
    graceful_cohort: str = "work",
    graceful_signal: bool = False,
    graceful_action: RuntimeTaskAction = RuntimeTaskAction.FINISH,
    graceful_deadline: float | None = None,
    graceful_hard_deadline: float | None = None,
    force_cohort: str = "work",
    force_signal: bool = False,
    force_action: RuntimeTaskAction = RuntimeTaskAction.CANCEL,
    force_deadline: float | None = None,
    force_hard_deadline: float | None = None,
) -> RuntimeTaskPolicy:
    return RuntimeTaskPolicy(
        graceful=RuntimeMemberPolicy(
            cohort=graceful_cohort,
            signal_token=graceful_signal,
            task_action=graceful_action,
            grace_deadline=graceful_deadline,
            hard_deadline=graceful_hard_deadline,
        ),
        force=RuntimeMemberPolicy(
            cohort=force_cohort,
            signal_token=force_signal,
            task_action=force_action,
            grace_deadline=force_deadline,
            hard_deadline=force_hard_deadline,
        ),
    )


def test_runtime_scope_child_hierarchy_shares_explicit_root_registry() -> None:
    root = _attached_root("session")
    router = root.create_child("audio-router")
    inline = router.create_child("inline-send")

    assert root.parent is None
    assert root.root is root
    assert root.children() == (router,)
    assert router.parent is root
    assert router.root is root
    assert router.children() == (inline,)
    assert inline.parent is router
    assert inline.root is root
    assert inline.owner_id == "session/audio-router/inline-send"
    assert inline.survivor_registry is root.survivor_registry


def test_runtime_scope_child_requires_attached_root_and_unique_name() -> None:
    with pytest.raises(RuntimeError, match="explicitly attached lifecycle root"):
        RuntimeScope().create_child("worker")

    root = _attached_root("session")
    root.create_child("worker")
    with pytest.raises(RuntimeError, match="already exists"):
        root.create_child("worker")


@pytest.mark.asyncio
async def test_runtime_scope_owned_child_charges_and_releases_both_quotas() -> None:
    supervisor = RuntimeSupervisor(capacity=2)
    root = _attached_root("session", supervisor=supervisor, survivor_capacity=2)
    child = root.create_child("inline-send")
    release = asyncio.Event()

    task = await child.start_owned_task("send", release.wait)

    registry = root.survivor_registry
    assert registry is not None
    assert root.tasks("send") == (task,)
    assert child.tasks("send") == (task,)
    assert registry.active_count == 1
    assert supervisor.active_count == 1

    release.set()
    await root.drain("send")

    assert root.empty
    assert registry.active_count == 0
    assert supervisor.active_count == 0


@pytest.mark.asyncio
async def test_runtime_scope_children_share_root_capacity() -> None:
    supervisor = RuntimeSupervisor(capacity=2)
    root = _attached_root("session", supervisor=supervisor, survivor_capacity=1)
    first = root.create_child("first")
    second = root.create_child("second")
    release = asyncio.Event()

    task = await first.start_owned_task("worker", release.wait)
    with pytest.raises(SurvivorCapacityError) as exc_info:
        await second.start_owned_task("worker", asyncio.Event().wait)
    assert exc_info.value.quota == "root"

    release.set()
    await task
    await root.drain()


@pytest.mark.asyncio
async def test_runtime_scope_roots_share_runtime_capacity() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    first_root = _attached_root("first-root", supervisor=supervisor, survivor_capacity=1)
    second_root = _attached_root("second-root", supervisor=supervisor, survivor_capacity=1)
    first = first_root.create_child("worker")
    second = second_root.create_child("worker")
    release = asyncio.Event()

    task = await first.start_owned_task("job", release.wait)
    with pytest.raises(SurvivorCapacityError) as exc_info:
        await second.start_owned_task("job", asyncio.Event().wait)
    assert exc_info.value.quota == "runtime"

    release.set()
    await task
    await first_root.drain()


@pytest.mark.asyncio
async def test_runtime_scope_adopts_task_when_caller_is_cancelled_during_start() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    root = _attached_root("session", supervisor=supervisor, survivor_capacity=1)
    child = root.create_child("inline-send")
    release = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def stubborn_work() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await release.wait()

    def cancel_during_factory() -> Coroutine[object, object, None]:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        return stubborn_work()

    async def start() -> None:
        await child.start_owned_task("send", cancel_during_factory)

    caller = asyncio.create_task(start())
    with pytest.raises(asyncio.CancelledError):
        await caller
    await cleanup_started.wait()

    retained = child.tasks("send")
    assert len(retained) == 1
    assert supervisor.active_count == 1
    assert supervisor.survivor_count == 1

    release.set()
    await root.drain("send")
    assert root.empty
    assert supervisor.active_count == 0


def test_runtime_member_policy_validates_deadline_contract() -> None:
    with pytest.raises(ValueError, match="must be non-negative"):
        RuntimeMemberPolicy(
            cohort="work",
            signal_token=False,
            task_action=RuntimeTaskAction.FINISH,
            grace_deadline=-0.1,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        RuntimeMemberPolicy(
            cohort="work",
            signal_token=False,
            task_action=RuntimeTaskAction.FINISH,
            grace_deadline=0.2,
            hard_deadline=0.1,
        )


@pytest.mark.asyncio
async def test_runtime_scope_policy_requires_signal_and_owned_hard_deadline() -> None:
    root = _attached_root("session")
    signal_policy = _task_policy(graceful_signal=True)
    hard_policy = _task_policy(force_hard_deadline=0.1)

    missing_signal = asyncio.sleep(0)
    with pytest.raises(ValueError, match="token signal callback"):
        root.create_task("missing-signal", missing_signal, policy=signal_policy)
    assert missing_signal.cr_frame is None

    raw_hard_deadline = asyncio.sleep(0)
    with pytest.raises(ValueError, match="must start as owned tasks"):
        root.create_task("raw-hard-deadline", raw_hard_deadline, policy=hard_policy)
    assert raw_hard_deadline.cr_frame is None


@pytest.mark.asyncio
async def test_signal_cohort_is_token_selective_and_does_not_imply_task_cancel() -> None:
    root = _attached_root("session")
    tokens: list[str] = []
    release = asyncio.Event()
    text_policy = _task_policy(
        graceful_cohort="text",
        graceful_signal=True,
        graceful_action=RuntimeTaskAction.FINISH,
    )
    prompt_policy = _task_policy(graceful_cohort="prompt")

    text_task = root.create_task(
        "text",
        release.wait(),
        policy=text_policy,
        token_signal=lambda: tokens.append("text"),
    )
    prompt_task = root.create_task("prompt", release.wait(), policy=prompt_policy)

    signal = root.signal_cohort("text", force=False)

    assert signal.tasks == (text_task,)
    assert tokens == ["text"]
    assert not text_task.cancelling()
    assert not prompt_task.cancelling()

    release.set()
    await root.drain()


@pytest.mark.asyncio
async def test_runtime_task_policy_selects_independent_mode_cohorts() -> None:
    root = _attached_root("session")
    release = asyncio.Event()
    policy = _task_policy(graceful_cohort="prompt", force_cohort="pipeline")
    root.create_task("member", release.wait(), policy=policy)

    assert root.cohorts(force=False) == ("prompt",)
    assert root.cohorts(force=True) == ("pipeline",)

    release.set()
    await root.drain()


@pytest.mark.asyncio
async def test_graceful_finish_member_completes_without_token_or_task_cancellation() -> None:
    root = _attached_root("session")
    token_signalled = False
    release = asyncio.Event()
    policy = _task_policy(
        graceful_cohort="prompt",
        graceful_signal=False,
        graceful_action=RuntimeTaskAction.FINISH,
        force_cohort="prompt",
        force_signal=True,
    )

    def signal_token() -> None:
        nonlocal token_signalled
        token_signalled = True

    prompt = root.create_task(
        "prompt",
        release.wait(),
        policy=policy,
        token_signal=signal_token,
    )
    closing = asyncio.create_task(root.close(phases=("prompt",)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not token_signalled
    assert not prompt.cancelling()
    assert not closing.done()

    release.set()
    assert await closing is RuntimeScopeState.CLOSED
    assert root.state is RuntimeScopeState.CLOSED


@pytest.mark.asyncio
async def test_force_close_signals_entire_cohort_before_awaiting_any_member() -> None:
    root = _attached_root("session")
    token_events: list[str] = []
    cleanup_observations: list[tuple[str, bool]] = []
    tasks: dict[str, asyncio.Task[None]] = {}
    policy = _task_policy(
        force_cohort="pipeline",
        force_signal=True,
        force_action=RuntimeTaskAction.CANCEL,
    )

    async def member(label: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            sibling = "tts" if label == "pipeline" else "pipeline"
            cleanup_observations.append((label, tasks[sibling].cancelling() > 0))
            raise

    for label in ("pipeline", "tts"):
        tasks[label] = root.create_task(
            label,
            member(label),
            policy=policy,
            token_signal=lambda label=label: token_events.append(label),
        )
    await asyncio.sleep(0)

    assert await root.close(force=True, phases=("pipeline",)) is RuntimeScopeState.CLOSED

    assert set(token_events) == {"pipeline", "tts"}
    assert len(cleanup_observations) == 2
    assert all(sibling_was_signalled for _label, sibling_was_signalled in cleanup_observations)


@pytest.mark.asyncio
async def test_close_respects_explicit_phase_order_without_total_ordering_siblings() -> None:
    root = _attached_root("session")
    events: list[str] = []

    async def member(label: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{label}:drained")

    prompt_policy = _task_policy(force_cohort="prompt", force_signal=True)
    pipeline_policy = _task_policy(force_cohort="pipeline", force_signal=True)
    root.create_task(
        "prompt",
        member("prompt"),
        policy=prompt_policy,
        token_signal=lambda: events.append("prompt:signal"),
    )
    for label in ("pipeline", "tts"):
        root.create_task(
            label,
            member(label),
            policy=pipeline_policy,
            token_signal=lambda label=label: events.append(f"{label}:signal"),
        )
    await asyncio.sleep(0)

    await root.close(force=True, phases=("prompt", "pipeline"))

    pipeline_signals = {events.index("pipeline:signal"), events.index("tts:signal")}
    assert events.index("prompt:drained") < min(pipeline_signals)
    assert max(pipeline_signals) < events.index("pipeline:drained")
    assert max(pipeline_signals) < events.index("tts:drained")


@pytest.mark.asyncio
async def test_signal_failure_does_not_skip_sibling_signal_or_drain() -> None:
    root = _attached_root("session")
    events: list[str] = []
    policy = _task_policy(force_signal=True)

    def fail_signal() -> None:
        events.append("first:signal")
        raise RuntimeError("token signal failed")

    root.create_task(
        "first",
        asyncio.Event().wait(),
        policy=policy,
        token_signal=fail_signal,
    )
    root.create_task(
        "second",
        asyncio.Event().wait(),
        policy=policy,
        token_signal=lambda: events.append("second:signal"),
    )
    await asyncio.sleep(0)

    signal = root.signal_cohort("work", force=True)
    with pytest.raises(RuntimeError, match="token signal failed"):
        await root.drain_cohort(signal)

    assert events == ["first:signal", "second:signal"]
    assert root.empty


@pytest.mark.asyncio
async def test_finish_policy_propagates_failure_after_draining_siblings() -> None:
    root = _attached_root("session")
    sibling_drained = asyncio.Event()
    policy = _task_policy(graceful_action=RuntimeTaskAction.FINISH)

    async def fail() -> None:
        raise RuntimeError("member failed")

    async def sibling() -> None:
        await asyncio.sleep(0)
        sibling_drained.set()

    root.create_task("failure", fail(), policy=policy)
    root.create_task("sibling", sibling(), policy=policy)

    signal = root.signal_cohort("work", force=False)
    with pytest.raises(RuntimeError, match="member failed"):
        await root.drain_cohort(signal)

    assert sibling_drained.is_set()
    assert root.empty


@pytest.mark.asyncio
async def test_cancel_policy_swallows_cancellation_cleanup_failure() -> None:
    root = _attached_root("session")
    policy = _task_policy(force_action=RuntimeTaskAction.CANCEL)

    async def fail_during_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    root.create_task("failure", fail_during_cancel(), policy=policy)
    await asyncio.sleep(0)

    signal = root.signal_cohort("work", force=True)
    await root.drain_cohort(signal)

    assert root.empty


@pytest.mark.asyncio
async def test_grace_deadline_escalates_finish_member_to_task_cancellation() -> None:
    root = _attached_root("session")
    cancelled = asyncio.Event()
    policy = _task_policy(
        graceful_deadline=0.01,
        graceful_action=RuntimeTaskAction.FINISH,
    )

    async def work() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = root.create_task("work", work(), policy=policy)
    await asyncio.sleep(0)

    assert await root.close(phases=("work",)) is RuntimeScopeState.CLOSED
    assert task.cancelled()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_hard_deadline_parks_owned_survivor_and_completion_closes_scope() -> None:
    supervisor = RuntimeSupervisor(capacity=1)
    root = _attached_root("session", supervisor=supervisor, survivor_capacity=1)
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()
    policy = _task_policy(force_hard_deadline=0.01)

    async def stubborn_work() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()

    task = await root.start_owned_task("cleanup", stubborn_work, policy=policy)

    state = await root.close(force=True, phases=("work",))

    assert state is RuntimeScopeState.CLOSED_WITH_SURVIVORS
    assert root.state is RuntimeScopeState.CLOSED_WITH_SURVIVORS
    assert cancellation_seen.is_set()
    assert supervisor.survivor_count == 1
    assert root.tasks() == (task,)

    release.set()
    await task
    await asyncio.sleep(0)

    assert root.state is RuntimeScopeState.CLOSED
    assert root.empty
    assert supervisor.active_count == 0


@pytest.mark.asyncio
async def test_force_close_supersedes_unbounded_graceful_close() -> None:
    root = _attached_root("session")
    started = asyncio.Event()

    async def work() -> None:
        started.set()
        await asyncio.Event().wait()

    task = root.create_task("work", work(), policy=_task_policy())
    await started.wait()
    graceful = asyncio.create_task(root.close(phases=("work",)))
    while root.state is RuntimeScopeState.OPEN:
        await asyncio.sleep(0)
    assert not graceful.done()

    force_state = await root.close(
        force=True,
        phases=("work",),
        supersede_timeout=0.1,
    )

    assert force_state is RuntimeScopeState.CLOSED
    assert await graceful is RuntimeScopeState.CLOSED
    assert task.cancelled()


@pytest.mark.asyncio
async def test_concurrent_owned_close_callers_are_both_detached_from_force_cohort() -> None:
    root = _attached_root("session")
    begin = asyncio.Event()

    async def close_from_member() -> RuntimeScopeState:
        await begin.wait()
        return await root.close(force=True, phases=("work",))

    first = asyncio.create_task(close_from_member())
    second = asyncio.create_task(close_from_member())
    policy = _task_policy(force_cohort="work")
    root.add_task("first-close", first, policy=policy)
    root.add_task("second-close", second, policy=policy)
    begin.set()

    states = await asyncio.gather(first, second)

    assert states == [RuntimeScopeState.CLOSED, RuntimeScopeState.CLOSED]
    assert not first.cancelled()
    assert not second.cancelled()
    assert root.empty


@pytest.mark.asyncio
async def test_thread_spawn_that_wins_admission_is_owned_by_later_close() -> None:
    root = _attached_root("session")
    root.create_task("bind-loop", asyncio.sleep(0))
    await root.drain("bind-loop")
    release = asyncio.Event()
    submitted: list = []

    def submit_from_thread() -> None:
        submitted.append(
            root.spawn_from_sync(
                "thread-worker",
                release.wait,
                policy=_task_policy(force_cohort="work"),
            )
        )

    thread = Thread(target=submit_from_thread)
    thread.start()
    thread.join()
    closing = asyncio.create_task(root.close(force=True, phases=("work",)))
    spawned = await asyncio.wrap_future(submitted[0])

    assert await closing is RuntimeScopeState.CLOSED
    assert spawned.cancelled()


@pytest.mark.asyncio
async def test_cancelled_thread_spawn_handle_does_not_invoke_factory() -> None:
    root = _attached_root("session")
    root.create_task("bind-loop", asyncio.sleep(0))
    await root.drain("bind-loop")
    factory_called = False

    def factory() -> Coroutine[object, object, None]:
        nonlocal factory_called
        factory_called = True
        return asyncio.sleep(0)

    handle = root.spawn_from_sync("thread-worker", factory)
    assert handle.cancel()
    await asyncio.sleep(0)

    assert not factory_called
    assert root.empty
    assert await root.close(force=True) is RuntimeScopeState.CLOSED


@pytest.mark.asyncio
async def test_close_rejects_phase_list_that_omits_selected_member() -> None:
    root = _attached_root("session")
    root.create_task("work", asyncio.Event().wait(), policy=_task_policy())

    with pytest.raises(ValueError, match="omit selected cohorts"):
        await root.close(force=True, phases=("other",))

    assert root.state is RuntimeScopeState.CLOSING
    assert await root.close(force=True, phases=("work",)) is RuntimeScopeState.CLOSED


@pytest.mark.asyncio
async def test_close_propagates_cancelled_finish_member_after_marking_terminal() -> None:
    root = _attached_root("session")
    task = root.create_task("work", asyncio.Event().wait(), policy=_task_policy())
    task.cancel()
    await asyncio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        await root.close(phases=("work",))

    assert root.state is RuntimeScopeState.CLOSED


@pytest.mark.asyncio
async def test_close_settles_remaining_cohorts_before_propagating_failure() -> None:
    root = _attached_root("session")
    later_finished = asyncio.Event()

    async def fail() -> None:
        raise RuntimeError("cohort failed")

    root.create_task("failing", fail(), policy=_task_policy(graceful_cohort="first"))
    root.create_task(
        "later",
        later_finished.wait(),
        policy=_task_policy(
            graceful_cohort="second",
            graceful_action=RuntimeTaskAction.CANCEL,
        ),
    )
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cohort failed"):
        await root.close(phases=("first", "second"))

    assert root.state is RuntimeScopeState.CLOSED
    assert root.empty


@pytest.mark.asyncio
async def test_closed_scope_rejects_all_admission_paths_without_running_factories() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id or "turn-1"

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            self.records.append({"name": name, "turn_id": turn_id, "data": data})

    root = _attached_root("session")
    root.create_task("bind-loop", asyncio.sleep(0))
    await root.close(force=True, phases=("default",))
    factory_called = False

    rejected = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="is closed"):
        root.create_task("rejected", rejected)
    assert rejected.cr_frame is None

    def factory() -> Coroutine[object, object, None]:
        nonlocal factory_called
        factory_called = True
        return asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="is closed"):
        await root.start_owned_task("owned-rejected", factory)
    assert not factory_called

    with pytest.raises(RuntimeError, match="is closed"):
        root.create_child("late-child")

    sink = RecordingSink()
    journaled = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="is closed"):
        root.create_journaled_task(journaled, name="journaled-rejected", journal_sink=sink)
    assert journaled.cr_frame is None
    assert sink.records == [
        {
            "name": "task_rejected",
            "turn_id": "turn-1",
            "data": {"task_name": "journaled-rejected", "reason": "scope_closed"},
        }
    ]

    handles: list = []

    def submit_from_thread() -> None:
        handles.append(root.spawn_from_sync("thread-rejected", factory))

    thread = Thread(target=submit_from_thread)
    thread.start()
    thread.join()
    with pytest.raises(RuntimeError, match="is closed"):
        handles[0].result()
    assert not factory_called


@pytest.mark.asyncio
async def test_runtime_scope_retains_task_values_and_errors_across_children() -> None:
    root = _attached_root("session")
    child = root.create_child("transport")

    async def fail() -> None:
        raise RuntimeError("send failed")

    success = child.create_task(
        "connect",
        asyncio.sleep(0, result="connected"),
        retain_result=True,
    )
    child.create_task("send", fail(), retain_result=True)
    await success
    with pytest.raises(RuntimeError, match="send failed"):
        await child.drain()

    results = root.terminal_results()
    assert {result.name for result in results} == {"connect", "send"}
    by_name = {result.name: result for result in results}
    assert by_name["connect"].owner_id == "session/transport"
    assert by_name["connect"].status is RuntimeResultStatus.COMPLETED
    assert by_name["connect"].unwrap() == "connected"
    assert by_name["send"].status is RuntimeResultStatus.RAISED
    with pytest.raises(RuntimeError, match="send failed"):
        by_name["send"].unwrap()

    assert root.pop_terminal_results("send") == (by_name["send"],)
    assert root.terminal_results() == (by_name["connect"],)


@pytest.mark.asyncio
async def test_force_cancel_retains_cleanup_failure_even_when_close_suppresses_it() -> None:
    root = _attached_root("session")

    async def fail_during_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    root.create_task(
        "worker",
        fail_during_cancel(),
        policy=_task_policy(force_action=RuntimeTaskAction.CANCEL),
        retain_result=True,
    )
    await asyncio.sleep(0)

    assert await root.close(force=True, phases=("work",)) is RuntimeScopeState.CLOSED

    result = root.terminal_results("worker")[0]
    assert result.status is RuntimeResultStatus.RAISED
    with pytest.raises(RuntimeError, match="cleanup failed"):
        result.unwrap()


@pytest.mark.asyncio
async def test_cohort_signal_reports_selected_task_actions() -> None:
    root = _attached_root("session")
    blocker = asyncio.Event()
    root.create_task(
        "finish",
        blocker.wait(),
        policy=_task_policy(force_action=RuntimeTaskAction.FINISH),
    )
    root.create_task(
        "cancel",
        blocker.wait(),
        policy=_task_policy(force_action=RuntimeTaskAction.CANCEL),
    )

    finish = root.signal_cohort("work", force=True)

    assert finish.includes_action(RuntimeTaskAction.FINISH)
    assert finish.includes_action(RuntimeTaskAction.CANCEL)

    blocker.set()
    await root.drain_cohort(finish)


@pytest.mark.asyncio
async def test_parked_owned_member_retains_result_when_it_eventually_settles() -> None:
    root = _attached_root("session", survivor_capacity=1)
    release = asyncio.Event()
    policy = _task_policy(force_hard_deadline=0.01)

    async def stubborn_work() -> str:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return "settled"

    task = await root.start_owned_task(
        "cleanup",
        stubborn_work,
        policy=policy,
        retain_result=True,
    )
    assert (
        await root.close(force=True, phases=("work",)) is RuntimeScopeState.CLOSED_WITH_SURVIVORS
    )
    assert root.terminal_results() == ()

    release.set()
    assert await task == "settled"
    await asyncio.sleep(0)

    result = root.terminal_results("cleanup")[0]
    assert result.status is RuntimeResultStatus.COMPLETED
    assert result.unwrap() == "settled"


@pytest.mark.asyncio
async def test_runtime_scope_finalizer_registration_rejects_duplicates_and_cohort_collisions() -> (
    None
):
    root = _attached_root("session")
    root.add_finalizer("disconnect", lambda: asyncio.sleep(0))

    with pytest.raises(RuntimeError, match="already exists"):
        root.create_child("transport").add_finalizer(
            "disconnect",
            lambda: asyncio.sleep(0),
        )

    rejected = asyncio.sleep(0)
    collision_policy = _task_policy(
        graceful_cohort="disconnect",
        force_cohort="disconnect",
    )
    with pytest.raises(RuntimeError, match="collides with a finalizer"):
        root.create_task("worker", rejected, policy=collision_policy)
    assert rejected.cr_frame is None

    other = _attached_root("other")
    other.create_task("worker", asyncio.sleep(0), policy=_task_policy())
    with pytest.raises(RuntimeError, match="collides with a task cohort"):
        other.add_finalizer("work", lambda: asyncio.sleep(0))
    await other.cancel_and_drain()


@pytest.mark.asyncio
async def test_run_finalizer_shares_attempt_without_closing_task_admission() -> None:
    root = _attached_root("session")
    child = root.create_child("provider")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def cleanup() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "closed"

    child.add_finalizer("provider-close", cleanup)
    first = asyncio.create_task(root.run_finalizer("provider-close"))
    await started.wait()
    second = asyncio.create_task(child.run_finalizer("provider-close"))
    await asyncio.sleep(0)

    assert calls == 1

    release.set()
    await asyncio.gather(first, second)

    admitted = child.create_task("after-finalize", asyncio.sleep(0))
    await child.drain("after-finalize")
    assert admitted.done()
    assert root.state is RuntimeScopeState.OPEN
    results = root.terminal_results("provider-close")
    assert len(results) == 1
    assert results[0].status is RuntimeResultStatus.COMPLETED
    assert results[0].unwrap() == "closed"

    assert await root.close() is RuntimeScopeState.CLOSED
    assert calls == 1


@pytest.mark.asyncio
async def test_run_finalizer_retains_failure_and_retries_factory() -> None:
    root = _attached_root("session")
    attempts = 0

    async def cleanup() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider close failed")

    root.add_finalizer("provider-close", cleanup)

    with pytest.raises(RuntimeError, match="provider close failed"):
        await root.run_finalizer("provider-close")

    assert root.state is RuntimeScopeState.OPEN
    assert root.terminal_results("provider-close")[0].status is RuntimeResultStatus.RAISED

    await root.run_finalizer("provider-close")

    assert attempts == 2
    assert [result.status for result in root.terminal_results("provider-close")] == [
        RuntimeResultStatus.RAISED,
        RuntimeResultStatus.COMPLETED,
    ]
    assert await root.close() is RuntimeScopeState.CLOSED
    assert attempts == 2


@pytest.mark.asyncio
async def test_run_finalizer_rejects_new_attempt_after_close_starts() -> None:
    root = _attached_root("session")
    release = asyncio.Event()
    calls = 0

    async def finalizer() -> None:
        nonlocal calls
        calls += 1

    root.create_task("work", release.wait())
    root.add_finalizer("provider-close", finalizer)
    closing = asyncio.create_task(root.close(phases=("default", "provider-close")))
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="is closing"):
        await root.run_finalizer("provider-close")

    release.set()
    assert await closing is RuntimeScopeState.CLOSED
    assert calls == 1


@pytest.mark.asyncio
async def test_close_runs_finalizers_at_explicit_positions_between_cohorts() -> None:
    root = _attached_root("session")
    events: list[str] = []

    async def member(label: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{label}:drained")

    async def disconnect() -> str:
        events.append("disconnect")
        return "done"

    root.create_task(
        "pipeline",
        member("pipeline"),
        policy=_task_policy(force_cohort="pipeline"),
    )
    root.create_task(
        "provider",
        member("provider"),
        policy=_task_policy(force_cohort="provider"),
    )
    root.add_finalizer("disconnect", disconnect)
    await asyncio.sleep(0)

    state = await root.close(
        force=True,
        phases=("pipeline", "disconnect", "provider"),
    )

    assert state is RuntimeScopeState.CLOSED
    assert events.index("pipeline:drained") < events.index("disconnect")
    assert events.index("disconnect") < events.index("provider:drained")
    result = root.terminal_results("disconnect")[0]
    assert result.kind is RuntimeMemberKind.FINALIZER
    assert result.status is RuntimeResultStatus.COMPLETED
    assert result.unwrap() == "done"


@pytest.mark.asyncio
async def test_failed_finalizer_retains_error_and_retries_without_rerunning_successes() -> None:
    root = _attached_root("session")
    attempts = 0
    successful_calls = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("disconnect failed")
        return "recovered"

    async def successful() -> None:
        nonlocal successful_calls
        successful_calls += 1

    root.add_finalizer("flaky", flaky)
    root.add_finalizer("successful", successful)

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await root.close(phases=("flaky", "successful"))
    assert root.state is RuntimeScopeState.CLOSING
    assert successful_calls == 0
    first = root.terminal_results("flaky")[0]
    assert first.status is RuntimeResultStatus.RAISED

    assert await root.close(phases=("flaky", "successful")) is RuntimeScopeState.CLOSED
    assert attempts == 2
    assert successful_calls == 1
    results = root.terminal_results("flaky")
    assert [result.status for result in results] == [
        RuntimeResultStatus.RAISED,
        RuntimeResultStatus.COMPLETED,
    ]
    assert results[1].unwrap() == "recovered"


@pytest.mark.asyncio
async def test_force_supersession_reuses_in_flight_finalizer_task() -> None:
    root = _attached_root("session")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def finalizer() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    root.add_finalizer("disconnect", finalizer)
    graceful = asyncio.create_task(root.close(phases=("disconnect",)))
    await started.wait()

    force = asyncio.create_task(
        root.close(
            force=True,
            phases=("disconnect",),
            supersede_timeout=0.1,
        )
    )
    await asyncio.sleep(0)
    assert calls == 1

    release.set()
    assert await force is RuntimeScopeState.CLOSED
    assert await graceful is RuntimeScopeState.CLOSED
    assert calls == 1
    assert len(root.terminal_results("disconnect")) == 1


@pytest.mark.asyncio
async def test_superseded_finalizer_retains_error_when_replacement_fails_before_join() -> None:
    root = _attached_root("session")
    started = asyncio.Event()
    release = asyncio.Event()

    async def finalizer() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("detached disconnect failed")

    root.add_finalizer("disconnect", finalizer)
    graceful = asyncio.create_task(root.close(phases=("disconnect",)))
    await started.wait()
    force = asyncio.create_task(
        root.close(
            force=True,
            phases=("missing",),
            supersede_timeout=0.1,
        )
    )

    with pytest.raises(ValueError, match="omit selected cohorts"):
        await force
    with pytest.raises(ValueError, match="omit selected cohorts"):
        await graceful

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    results = root.terminal_results("disconnect")
    assert len(results) == 1
    assert results[0].status is RuntimeResultStatus.RAISED
    with pytest.raises(RuntimeError, match="detached disconnect failed"):
        results[0].unwrap()

    # A later close observes the already-retained attempt without duplicating
    # its evidence; a subsequent close may then retry the failed finalizer.
    with pytest.raises(RuntimeError, match="detached disconnect failed"):
        await root.close(phases=("disconnect",))
    assert root.terminal_results("disconnect") == results


@pytest.mark.asyncio
async def test_cancelled_finalizer_is_retained_and_propagated_as_cancellation() -> None:
    root = _attached_root("session")

    async def cancelled() -> None:
        raise asyncio.CancelledError

    root.add_finalizer("disconnect", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await root.close(phases=("disconnect",))

    result = root.terminal_results("disconnect")[0]
    assert result.status is RuntimeResultStatus.CANCELLED
    with pytest.raises(asyncio.CancelledError):
        result.unwrap()


@pytest.mark.asyncio
async def test_finalizer_admission_closes_with_scope() -> None:
    root = _attached_root("session")
    await root.close(force=True)

    with pytest.raises(RuntimeError, match="is closed"):
        root.add_finalizer("late", lambda: asyncio.sleep(0))


def test_create_journaled_task_records_lifecycle_via_structural_sink() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id or "turn-1"

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            self.records.append({"name": name, "turn_id": turn_id, "data": data})

    sink = RecordingSink()
    # A duck-typed sink satisfies the runtime-owned protocol structurally,
    # so the runtime layer needs no dependency on the session package.
    assert isinstance(sink, JournalSink)

    scope = RuntimeScope()

    async def work() -> str:
        return "ok"

    async def run() -> None:
        task = scope.create_journaled_task(work(), name="job", journal_sink=sink)
        await scope.drain("job")
        assert task.result() == "ok"

    asyncio.run(run())

    names = [r["name"] for r in sink.records]
    assert names == ["task_scheduled", "task_completed"]
    assert all(r["turn_id"] == "turn-1" for r in sink.records)


def test_runtime_scope_closes_coroutine_when_task_creation_fails() -> None:
    scope = RuntimeScope()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(RuntimeError, match="no running event loop"):
        scope.create_task("worker", coro)

    assert coro.cr_frame is None


def test_journaled_runtime_scope_does_not_record_or_leak_when_task_creation_fails() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            self.records.append({"name": name, "turn_id": turn_id, "data": data})

    scope = RuntimeScope()
    sink = RecordingSink()

    async def work() -> None:
        pass

    coro = work()
    with pytest.raises(RuntimeError, match="no running event loop"):
        scope.create_journaled_task(coro, name="worker", journal_sink=sink)

    assert coro.cr_frame is None
    assert sink.records == []


@pytest.mark.asyncio
async def test_journaled_runtime_scope_cancels_task_when_journaling_setup_fails() -> None:
    class FailingSink:
        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            raise RuntimeError("journal unavailable")

    scope = RuntimeScope()
    started = asyncio.Event()

    async def work() -> None:
        started.set()

    coro = work()
    with pytest.raises(RuntimeError, match="journal unavailable"):
        scope.create_journaled_task(coro, name="worker", journal_sink=FailingSink())

    await asyncio.sleep(0)
    assert not started.is_set()
    assert coro.cr_frame is None
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_rejects_empty_name_before_scheduling_coroutine() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()

    async def work() -> None:
        started.set()

    coro = work()
    with pytest.raises(ValueError, match="must be non-empty"):
        scope.create_task("", coro)

    await asyncio.sleep(0)
    assert not started.is_set()
    assert coro.cr_frame is None
    assert scope.empty


@pytest.mark.asyncio
async def test_journaled_runtime_task_rejects_empty_name_before_recording_or_scheduling() -> None:
    class RecordingSink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def current_turn_id(self, turn_id: str | None = None) -> str | None:
            return turn_id

        def append_record(
            self,
            *,
            name: str,
            turn_id: str | None = None,
            data: dict[str, object] | None = None,
        ) -> None:
            self.records.append({"name": name, "turn_id": turn_id, "data": data})

    scope = RuntimeScope()
    sink = RecordingSink()
    started = asyncio.Event()

    async def work() -> None:
        started.set()

    coro = work()
    with pytest.raises(ValueError, match="must be non-empty"):
        scope.create_journaled_task(coro, name="", journal_sink=sink)

    await asyncio.sleep(0)
    assert not started.is_set()
    assert coro.cr_frame is None
    assert sink.records == []
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drains_completed_task() -> None:
    scope = RuntimeScope()

    async def complete() -> str:
        await asyncio.sleep(0)
        return "done"

    task = scope.create_task("worker", complete())

    await scope.drain("worker")

    assert task.done()
    assert task.result() == "done"
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_cancels_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()
    never_released = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await never_released.wait()
        finally:
            cleaned_up.set()

    task = scope.create_task("worker", wait_forever())
    await started.wait()

    await scope.cancel_and_drain("worker")

    assert task.cancelled()
    assert cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_ignores_preexisting_cancellation_count() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def drain_after_caught_cancellation() -> int:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        await scope.cancel_and_drain("worker")
        return current.cancelling()

    caller = asyncio.create_task(drain_after_caught_cancellation())

    assert await caller == 1
    assert owned.cancelled()
    assert cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_preserves_cancellation_pending_at_entry() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        await asyncio.Event().wait()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def cancel_before_drain() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await scope.cancel_and_drain("worker")

    caller = asyncio.create_task(cancel_before_drain())

    with pytest.raises(asyncio.CancelledError):
        await caller
    await asyncio.gather(owned, return_exceptions=True)
    await scope.cancel_and_drain("worker")
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_propagates_new_cancellation_count() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    awaiting_drain = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    owned = scope.create_task("worker", wait_forever())
    await started.wait()

    async def drain_after_caught_cancellation() -> None:
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert current.cancelling() == 1

        awaiting_drain.set()
        await scope.cancel_and_drain("worker")

    caller = asyncio.create_task(drain_after_caught_cancellation())
    await awaiting_drain.wait()
    await cleanup_started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.cancelling() == 2

    release_cleanup.set()
    await asyncio.gather(owned, return_exceptions=True)
    await scope.cancel_and_drain("worker")
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_detaches_caller_and_cancels_siblings() -> None:
    scope = RuntimeScope()
    sibling_started = asyncio.Event()
    sibling_cleaned_up = asyncio.Event()
    caller_continued = asyncio.Event()

    async def sibling() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cleaned_up.set()

    sibling_task = scope.create_task("sibling", sibling())
    await sibling_started.wait()

    async def cancel_from_owned_task() -> None:
        current = asyncio.current_task()
        assert current is not None
        scope.add_task("owner", current)

        await scope.cancel_and_drain()
        await asyncio.sleep(0)
        caller_continued.set()

    owner_task = asyncio.create_task(cancel_from_owned_task())
    await owner_task

    assert not owner_task.cancelled()
    assert caller_continued.is_set()
    assert sibling_task.cancelled()
    assert sibling_cleaned_up.is_set()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_discard_detaches_without_cancelling_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def wait_for_release() -> str:
        started.set()
        await release.wait()
        completed.set()
        return "done"

    task = scope.create_task("worker", wait_for_release())
    await started.wait()

    scope.discard(task)

    assert scope.empty
    assert not task.cancelled()
    assert not task.done()

    release.set()
    assert await task == "done"
    assert completed.is_set()


@pytest.mark.asyncio
async def test_runtime_scope_discard_allows_current_task_to_detach_itself() -> None:
    scope = RuntimeScope()

    async def self_detach() -> None:
        task = asyncio.current_task()
        assert task is not None
        scope.add_task("self", task)
        assert scope.tasks("self") == (task,)

        scope.discard(task)

        assert scope.empty

    await self_detach()


@pytest.mark.asyncio
async def test_runtime_scope_drain_propagates_task_exceptions() -> None:
    scope = RuntimeScope()

    async def fail() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    scope.create_task("worker", fail())

    with pytest.raises(RuntimeError, match="boom"):
        await scope.drain("worker")

    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_observes_completed_task_exceptions() -> None:
    scope = RuntimeScope()

    async def fail() -> None:
        raise RuntimeError("boom")

    task = scope.create_task("worker", fail())
    while not task.done():
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="boom"):
        await scope.drain("worker")

    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_can_suppress_member_errors_without_cancelling_work() -> None:
    scope = RuntimeScope()
    sibling_finished = False

    async def fail() -> None:
        raise RuntimeError("emit failed")

    async def sibling() -> None:
        nonlocal sibling_finished
        await asyncio.sleep(0)
        sibling_finished = True

    scope.create_task("emit", fail())
    scope.create_task("emit", sibling())

    await scope.drain("emit", suppress_errors=True)

    assert sibling_finished
    assert scope.empty


@pytest.mark.asyncio
async def test_suppressing_member_errors_still_propagates_drain_caller_cancellation() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release = asyncio.Event()

    async def member() -> None:
        started.set()
        await release.wait()

    task = scope.create_task("emit", member())
    await started.wait()
    draining = asyncio.create_task(scope.drain("emit", suppress_errors=True))
    await asyncio.sleep(0)
    draining.cancel()

    with pytest.raises(asyncio.CancelledError):
        await draining
    assert not task.cancelled()

    release.set()
    await scope.drain("emit", suppress_errors=True)


@pytest.mark.asyncio
async def test_runtime_scope_cancel_and_drain_preserves_caller_cancellation() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_started = asyncio.Event()

    async def stubborn_cancel_cleanup() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    task = scope.create_task("worker", stubborn_cancel_cleanup())
    await started.wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(scope.cancel_and_drain("worker"), timeout=0.01)

    assert cleanup_started.is_set()
    assert not scope.empty

    release_cleanup.set()
    await scope.cancel_and_drain("worker")

    assert task.cancelled()
    assert scope.empty


@pytest.mark.asyncio
async def test_runtime_scope_drain_preserves_caller_cancellation_without_cancelling_task() -> None:
    scope = RuntimeScope()
    started = asyncio.Event()
    release = asyncio.Event()

    async def wait_for_release() -> str:
        started.set()
        await release.wait()
        return "done"

    task = scope.create_task("worker", wait_for_release())
    await started.wait()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(scope.drain("worker"), timeout=0.01)

    assert not task.cancelled()
    assert not scope.empty

    release.set()
    await scope.drain("worker")

    assert task.result() == "done"
    assert scope.empty
