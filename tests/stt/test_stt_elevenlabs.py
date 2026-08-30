"""Tests for the ElevenLabs STT provider."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.events import STTEventType
from easycat.stt import base, elevenlabs_provider
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from tests.stt.helpers import collect_stt_events, generate_pcm_sine, make_audio_chunks


class MockWebSocket:
    """Mock WebSocket connection for ElevenLabs tests."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = messages or []
        self.sent: list[str] = []
        self._closed = False
        self._iter_index = 0

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._iter_index >= len(self.messages):
            raise StopAsyncIteration
        msg = self.messages[self._iter_index]
        self._iter_index += 1
        return msg


def _el_transcript(
    text: str,
    is_final: bool = False,
    confidence: float | None = None,
    language: str | None = None,
    words: list[dict] | None = None,
) -> str:
    """Create an ElevenLabs-format transcript message."""
    msg_type = "committed_transcript" if is_final else "partial_transcript"
    msg: dict = {"message_type": msg_type, "text": text}
    if confidence is not None:
        msg["confidence"] = confidence
    if language:
        msg["language_code"] = language
    if words:
        msg["message_type"] = "committed_transcript_with_timestamps"
        msg["words"] = words
    return json.dumps(msg)


def _make_el_stt_realtime(
    messages: list[str] | None = None,
) -> tuple[ElevenLabsSTT, MockWebSocket, dict[str, str]]:
    """Create an ElevenLabs realtime STT with a mocked WebSocket."""
    ws = MockWebSocket(messages or [])
    connect_meta: dict[str, str] = {}

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        connect_meta["url"] = url
        return ws

    config = ElevenLabsSTTConfig(
        api_key="test-key",
        mode="realtime",
        ws_connect=mock_connect,
        final_transcript_timeout_s=0.05,
    )
    return ElevenLabsSTT(config), ws, connect_meta


def _make_mock_http_client(
    text: str = "hello world", confidence: float | None = None
) -> httpx.AsyncClient:
    """Create a mock httpx.AsyncClient for batch transcription."""
    body: dict = {"text": text}
    if confidence is not None:
        body["confidence"] = confidence
    mock_response = httpx.Response(
        status_code=200,
        json=body,
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


# ── Realtime mode ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_realtime_receives_final():
    messages = [_el_transcript("hello world", is_final=True)]
    stt, _ws, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "hello world"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_partial_and_final():
    messages = [
        _el_transcript("hel", is_final=False),
        _el_transcript("hello world", is_final=True),
    ]
    stt, _ws, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 2
    assert events[0].type == STTEventType.PARTIAL
    assert events[1].type == STTEventType.FINAL


@pytest.mark.asyncio
async def test_elevenlabs_realtime_connects_with_query_params():
    stt, _ws, connect_meta = _make_el_stt_realtime([])

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    await stt.end_stream()

    url = connect_meta["url"]
    assert "/v1/speech-to-text/realtime?" in url
    assert "model_id=scribe_v2_realtime" in url
    assert "audio_format=pcm_16000" in url
    # Built-in VAD commit strategy is the default for the realtime model.
    assert "commit_strategy=vad" in url


@pytest.mark.asyncio
async def test_elevenlabs_realtime_vad_tuning_in_query_params():
    ws = MockWebSocket([])
    connect_meta: dict[str, str] = {}

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        connect_meta["url"] = url
        return ws

    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        ws_connect=mock_connect,
        realtime_commit_strategy="vad",
        realtime_vad_threshold=0.5,
        realtime_vad_silence_threshold_secs=1.2,
        realtime_min_speech_duration_ms=120,
        realtime_min_silence_duration_ms=80,
    )
    stt = ElevenLabsSTT(config)
    await stt.start_stream()
    await stt.end_stream()

    url = connect_meta["url"]
    assert "commit_strategy=vad" in url
    assert "vad_threshold=0.5" in url
    assert "vad_silence_threshold_secs=1.2" in url
    assert "min_speech_duration_ms=120" in url
    assert "min_silence_duration_ms=80" in url


@pytest.mark.asyncio
async def test_elevenlabs_realtime_manual_strategy_omits_vad_params():
    ws = MockWebSocket([])
    connect_meta: dict[str, str] = {}

    async def mock_connect(url: str, **kwargs) -> MockWebSocket:
        connect_meta["url"] = url
        return ws

    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        ws_connect=mock_connect,
        realtime_commit_strategy="manual",
        realtime_vad_threshold=0.5,
    )
    stt = ElevenLabsSTT(config)
    await stt.start_stream()
    await stt.end_stream()

    url = connect_meta["url"]
    assert "commit_strategy=manual" in url
    assert "vad_threshold" not in url


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_audio_as_base64():
    stt, ws, _ = _make_el_stt_realtime([])

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm, chunk_duration_ms=100)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)
    await stt.end_stream()

    # Audio messages should be base64-encoded JSON
    audio_msgs = [json.loads(s) for s in ws.sent if '"input_audio_chunk"' in s]
    assert len(audio_msgs) >= 1
    assert audio_msgs[0]["message_type"] == "input_audio_chunk"
    assert "audio_base_64" in audio_msgs[0]
    assert audio_msgs[0]["commit"] is False
    assert audio_msgs[0]["sample_rate"] == 16000


@pytest.mark.asyncio
async def test_elevenlabs_realtime_sends_stop():
    stt, ws, _ = _make_el_stt_realtime([])

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    await stt.end_stream()

    json_sent = [json.loads(s) for s in ws.sent]
    stop_msgs = [
        m
        for m in json_sent
        if m.get("message_type") == "input_audio_chunk" and m.get("commit") is True
    ]
    assert len(stop_msgs) == 1


@pytest.mark.asyncio
async def test_elevenlabs_realtime_close_failure_blocks_restart_until_cleanup_retry(
    monkeypatch,
):
    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="test-key", mode="realtime"))
    close_calls = 0

    async def fail_once_close(*, close_before_drain: bool = False) -> None:
        nonlocal close_calls
        assert close_before_drain is True
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("socket close failed")

    async def start_replacement() -> None:
        return None

    monkeypatch.setattr(stt, "_close_active_websocket", fail_once_close)
    monkeypatch.setattr(stt, "_on_start", start_replacement)
    stt._running = True

    with pytest.raises(RuntimeError, match="socket close failed"):
        await stt.end_stream()

    assert stt._failed_end_cleanup_pending is True
    assert close_calls == 1

    await stt.start_stream()

    assert close_calls == 2
    assert stt._failed_end_cleanup_pending is False
    assert stt._running is True
    await stt.end_stream()


@pytest.mark.asyncio
async def test_elevenlabs_realtime_commit_segment_keeps_stream_open_for_later_audio():
    messages = [
        _el_transcript("hello", is_final=True),
        _el_transcript("world", is_final=True),
    ]
    stt, ws, _ = _make_el_stt_realtime(messages)

    collected = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    await stt.send_audio(chunk)
    assert await stt.commit_segment() is True
    await stt.send_audio(chunk)
    await stt.end_stream()
    await collect_task

    finals = [event.text for event in collected if event.type == STTEventType.FINAL]
    assert finals == ["hello", "world"]

    json_sent = [json.loads(s) for s in ws.sent]
    commit_msgs = [
        m
        for m in json_sent
        if m.get("message_type") == "input_audio_chunk" and m.get("commit") is True
    ]
    assert len(commit_msgs) == 2


def test_elevenlabs_realtime_url_carries_keyterms_and_no_verbatim():
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        realtime_keyterms=["EasyCat", "Cartesia"],
        realtime_no_verbatim=True,
    )
    url = ElevenLabsSTT(config)._build_realtime_ws_url()

    # keyterms is one repeated query param per term.
    assert url.count("keyterms=") == 2
    assert "keyterms=EasyCat" in url
    assert "keyterms=Cartesia" in url
    assert "no_verbatim=true" in url


def test_elevenlabs_realtime_url_omits_keyterms_and_verbatim_by_default():
    url = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))._build_realtime_ws_url()
    assert "keyterms=" not in url
    assert "no_verbatim" not in url


def test_elevenlabs_realtime_url_carries_language_detection_and_zero_retention():
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        realtime_include_language_detection=True,
        enable_logging=False,
    )
    url = ElevenLabsSTT(config)._build_realtime_ws_url()
    assert "include_language_detection=true" in url
    assert "enable_logging=false" in url
    # language_code is only delivered on *_with_timestamps events, so enabling
    # language detection must force include_timestamps on (else it's inert).
    assert "include_timestamps=true" in url


def test_elevenlabs_realtime_url_omits_logging_and_language_detection_by_default():
    # Defaults keep the server defaults: logging on, no language detection.
    url = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))._build_realtime_ws_url()
    assert "enable_logging" not in url
    assert "include_language_detection" not in url


@pytest.mark.asyncio
async def test_elevenlabs_batch_sends_zero_retention_flag():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(
        api_key="k", mode="batch", http_client=mock_client, enable_logging=False
    )
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    # enable_logging is a query parameter on /v1/speech-to-text, not a
    # multipart form field (where it would be ignored).
    call = mock_client.post.call_args
    assert call.kwargs["params"] == {"enable_logging": "false"}
    assert "enable_logging" not in call.kwargs.get("data", {})


@pytest.mark.asyncio
async def test_elevenlabs_batch_omits_logging_flag_by_default():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call = mock_client.post.call_args
    # No zero-retention requested → no enable_logging anywhere (server default).
    assert call.kwargs.get("params") is None
    assert "enable_logging" not in call.kwargs.get("data", {})


@pytest.mark.asyncio
async def test_elevenlabs_batch_api_error_is_emitted_before_propagation():
    from easycat.events import Error, ErrorStage, EventBus

    response = httpx.Response(
        status_code=503,
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=response)
    mock_client.aclose = AsyncMock()
    bus = EventBus()
    errors: list[Error] = []

    async def capture_error(event: Error) -> None:
        errors.append(event)

    bus.subscribe(Error, capture_error)
    stt = ElevenLabsSTT(
        ElevenLabsSTTConfig(
            api_key="k",
            mode="batch",
            max_retries=1,
            http_client=mock_client,
            event_bus=bus,
        )
    )
    await stt.start_stream()
    await stt.send_audio(make_audio_chunks(generate_pcm_sine(duration_ms=100))[0])

    with pytest.raises(httpx.HTTPStatusError):
        await stt.end_stream()
    await stt.close()

    assert len(errors) == 1
    assert errors[0].provider == "elevenlabs"
    assert errors[0].stage is ErrorStage.STT
    assert "http_status=503" in getattr(errors[0].exception, "__notes__", ())


def test_elevenlabs_rejects_too_many_keyterms():
    with pytest.raises(ValueError, match="at most 50 terms"):
        ElevenLabsSTTConfig(api_key="k", realtime_keyterms=[f"t{i}" for i in range(51)])


def test_elevenlabs_rejects_overlong_keyterm():
    with pytest.raises(ValueError, match="<= 20 characters"):
        ElevenLabsSTTConfig(api_key="k", realtime_keyterms=["x" * 21])


def test_elevenlabs_stt_config_rejects_negative_max_retries():
    with pytest.raises(ValueError, match="max_retries"):
        ElevenLabsSTTConfig(api_key="k", max_retries=-1)


@pytest.mark.parametrize(
    "limit_name",
    ["max_audio_chunk_bytes", "max_audio_buffer_bytes", "max_audio_duration_ms"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_elevenlabs_stt_config_rejects_nonfinite_audio_limits(
    limit_name: str,
    value: float | bool,
) -> None:
    with pytest.raises(ValueError, match="positive finite number"):
        ElevenLabsSTTConfig(api_key="k", **{limit_name: value})


@pytest.mark.parametrize("mode", ["", "realtim", "streaming", 1, None])
def test_elevenlabs_stt_config_rejects_unknown_mode(mode):
    with pytest.raises(ValueError, match=r"mode must be 'realtime' or 'batch'"):
        ElevenLabsSTTConfig(api_key="k", mode=mode)


@pytest.mark.parametrize("commit_strategy", ["", "vda", "auto", 1, None])
def test_elevenlabs_stt_config_rejects_unknown_commit_strategy(commit_strategy):
    with pytest.raises(ValueError, match=r"realtime_commit_strategy must be 'vad' or 'manual'"):
        ElevenLabsSTTConfig(api_key="k", realtime_commit_strategy=commit_strategy)


def test_elevenlabs_stt_config_normalizes_mode_and_commit_strategy():
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode=" ReAlTiMe ",
        realtime_commit_strategy=" VAD ",
    )

    assert config.mode == "realtime"
    assert config.realtime_commit_strategy == "vad"
    assert config.resolved_model == "scribe_v2_realtime"


def test_elevenlabs_config_final_timeout_default():
    """The bounded final-wait defaults to the module constant."""
    config = ElevenLabsSTTConfig(api_key="k")

    assert config.final_transcript_timeout_s == elevenlabs_provider._FINAL_TRANSCRIPT_TIMEOUT_S
    assert config.final_transcript_timeout_s == pytest.approx(5.0)


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf")])
def test_elevenlabs_config_rejects_invalid_final_timeout(timeout: object) -> None:
    with pytest.raises(ValueError, match="final_transcript_timeout_s"):
        ElevenLabsSTTConfig(api_key="k", final_transcript_timeout_s=timeout)


@pytest.mark.asyncio
async def test_elevenlabs_empty_committed_transcript_acknowledges_without_final():
    """A silence-only commit releases its waiter without emitting empty text."""
    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))
    final_received = asyncio.Event()
    stt._final_received = final_received
    stt._audio_epoch = 1
    stt._committed_through_epoch = 1
    stt._audio_pending_commit = False
    stt._manual_commit_inflight = 1

    stt._handle_json_message(
        {
            "message_type": "committed_transcript",
            "text": "",
        }
    )

    assert final_received.is_set()
    assert stt._manual_commit_inflight == 0
    assert stt._committed_through_epoch == 1
    assert stt._audio_pending_commit is False
    assert stt._event_queue.empty()


@pytest.mark.asyncio
async def test_elevenlabs_realtime_vad_commit_clears_pending_no_redundant_commit():
    # In VAD mode (the realtime default) the server emits committed_transcript
    # on its own. Once it does, _audio_pending_commit must be cleared so a
    # later end_stream does not issue a redundant manual commit and block for
    # the full final-transcript timeout waiting on a final that already came.
    ws = MockWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        ws_connect=mock_connect,
        realtime_commit_strategy="vad",
    )
    stt = ElevenLabsSTT(config)

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    assert stt._audio_pending_commit is True

    # The server transcribes (partial) then VAD-commits the segment it covers.
    stt._handle_json_message(json.loads(_el_transcript("hello", is_final=False)))
    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))
    assert stt._audio_pending_commit is False

    # Bounded so a regression (redundant commit waiting on a final) fails
    # loudly instead of hanging the suite for the full timeout.
    await asyncio.wait_for(stt.end_stream(), timeout=1.0)

    commit_frames = [
        m
        for m in (json.loads(s) for s in ws.sent if isinstance(s, str))
        if m.get("commit") is True
    ]
    assert commit_frames == []


@pytest.mark.asyncio
async def test_elevenlabs_realtime_vad_commit_without_partial_clears_pending():
    # ElevenLabs can emit a server-driven final without first sending a
    # partial_transcript. The final itself must acknowledge the current audio
    # epoch so end_stream does not send a redundant manual commit.
    ws = MockWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        ws_connect=mock_connect,
        realtime_commit_strategy="vad",
    )
    stt = ElevenLabsSTT(config)

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)
    assert stt._audio_pending_commit is True

    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))
    assert stt._audio_pending_commit is False
    assert stt._committed_through_epoch == stt._audio_epoch

    await asyncio.wait_for(stt.end_stream(), timeout=1.0)

    commit_frames = [
        m
        for m in (json.loads(s) for s in ws.sent if isinstance(s, str))
        if m.get("commit") is True
    ]
    assert commit_frames == []


@pytest.mark.asyncio
async def test_elevenlabs_realtime_vad_commit_without_partial_keeps_newer_audio_pending():
    # If newer audio was streamed before an older no-partial VAD final is
    # processed, the final must not acknowledge that trailing audio.
    config = ElevenLabsSTTConfig(api_key="k", mode="realtime")
    stt = ElevenLabsSTT(config)
    stt._ws = MockWebSocket([])
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    await stt._send_realtime(chunk)
    await stt._send_realtime(chunk)
    assert stt._audio_epoch == 2
    assert stt._audio_pending_commit is True

    stt._handle_json_message(json.loads(_el_transcript("segment one", is_final=True)))

    assert stt._committed_through_epoch == 1
    assert stt._audio_pending_commit is True


@pytest.mark.asyncio
async def test_elevenlabs_realtime_late_committed_does_not_drop_newer_audio(monkeypatch):
    # Race: commit_segment() then more audio is sent, then the *prior*
    # segment's committed_transcript arrives. The late ack must NOT clear the
    # newer audio's pending state, so end_stream still commits it.
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = MockWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    await stt.start_stream()
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    await stt.send_audio(chunk)  # segment 1 audio
    assert await stt.commit_segment() is True  # manual commit for segment 1
    await stt.send_audio(chunk)  # segment 2 audio (after the commit)
    assert stt._audio_pending_commit is True

    # The late committed_transcript for segment 1 arrives now.
    stt._handle_json_message(json.loads(_el_transcript("segment one", is_final=True)))

    # Segment 2 audio is still uncommitted — pending must survive.
    assert stt._audio_pending_commit is True

    await asyncio.wait_for(stt.end_stream(), timeout=2.0)

    # end_stream must have issued a commit to flush segment 2 (commit:true frame
    # beyond the one commit_segment() already sent).
    commit_frames = [
        m
        for m in (json.loads(s) for s in ws.sent if isinstance(s, str))
        if m.get("commit") is True
    ]
    assert len(commit_frames) == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_old_final_cannot_release_newer_end_commit():
    # A manual segment commit may still be awaiting its final when end_stream
    # commits newly arrived audio. Finals are FIFO but carry no request id, so
    # the older final must not release the newer end-of-turn wait and let the
    # provider close before the trailing transcript arrives.
    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))
    stt._ws = MockWebSocket([])
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    await stt._send_realtime(chunk)
    assert await stt._send_commit(wait_for_final=False) is True
    await stt._send_realtime(chunk)

    end_commit = asyncio.create_task(stt._send_commit(wait_for_final=True))
    await asyncio.sleep(0)
    assert stt._manual_commit_inflight == 2

    # This belongs to the earlier non-blocking segment commit.
    stt._handle_json_message(json.loads(_el_transcript("segment one", is_final=True)))
    await asyncio.sleep(0)
    assert not end_commit.done()

    stt._handle_json_message(json.loads(_el_transcript("segment two", is_final=True)))
    assert await end_commit is True

    finals = []
    while not stt._event_queue.empty():
        event = stt._event_queue.get_nowait()
        if event.type == STTEventType.FINAL:
            finals.append(event.text)
    assert finals == ["segment one", "segment two"]


@pytest.mark.asyncio
async def test_elevenlabs_realtime_vad_commit_keeps_pending_for_trailing_audio():
    # A VAD commit covers only what the server transcribed (up to the last
    # partial). Audio streamed after that must keep pending set so end_stream
    # still commits it — even if no later partial re-arms the flag first.
    config = ElevenLabsSTTConfig(api_key="k", mode="realtime")
    stt = ElevenLabsSTT(config)
    stt._ws = MockWebSocket([])  # let _send_realtime stream + bump the epoch
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]

    stt._final_received = None  # no manual commit in flight (pure VAD)
    # Segment 1 audio, a partial for it, then segment 2 audio (trailing).
    await stt._send_realtime(chunk)
    stt._handle_json_message(json.loads(_el_transcript("seg one", is_final=False)))
    await stt._send_realtime(chunk)
    assert stt._audio_pending_commit is True

    # Late VAD commit for segment 1 must NOT clear pending for segment 2.
    stt._handle_json_message(json.loads(_el_transcript("seg one", is_final=True)))
    assert stt._audio_pending_commit is True


@pytest.mark.asyncio
async def test_elevenlabs_realtime_partial_after_vad_commit_rearms_pending():
    # VAD auto-commit clears pending; if speech resumes (a new partial) the
    # pending flag must re-arm so the resumed audio is committed at end-of-turn.
    config = ElevenLabsSTTConfig(api_key="k", mode="realtime")
    stt = ElevenLabsSTT(config)

    stt._audio_pending_commit = True
    # Unsolicited VAD commit (no manual commit in flight) clears pending.
    stt._handle_json_message(json.loads(_el_transcript("first", is_final=True)))
    assert stt._audio_pending_commit is False

    # Speech resumes: a partial re-arms pending.
    stt._handle_json_message(json.loads(_el_transcript("second...", is_final=False)))
    assert stt._audio_pending_commit is True


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_confidence():
    messages = [_el_transcript("test", is_final=True, confidence=0.92)]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.92


@pytest.mark.asyncio
async def test_elevenlabs_realtime_with_word_timestamps():
    words = [
        {"text": "hello", "start": 0.0, "end": 0.3},
        {"text": "world", "start": 0.4, "end": 0.7},
    ]
    messages = [_el_transcript("hello world", is_final=True, words=words)]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].word_timestamps is not None
    assert len(events[0].word_timestamps) == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_ignores_non_transcript():
    messages = [
        json.dumps({"message_type": "session_started"}),
        _el_transcript("hello", is_final=True),
    ]
    stt, _, _ = _make_el_stt_realtime(messages)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1


class _BlockingWebSocket:
    """Mock WebSocket that yields a fixed set of messages then blocks.

    Unlike :class:`MockWebSocket`, the receive iterator does not end after
    the canned messages — it waits until ``close()`` is called. This keeps
    the provider's receive loop alive so a commit can genuinely time out
    waiting for a final that never arrives.
    """

    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []
        self._iter_index = 0
        self._closed = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self._closed.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._iter_index < len(self.messages):
            msg = self.messages[self._iter_index]
            self._iter_index += 1
            return msg
        await self._closed.wait()
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_elevenlabs_reconnect_releases_waiting_commit_with_partial_fallback():
    """A reconnect cannot leave an end-of-turn commit waiting for a lost final."""
    stt = ElevenLabsSTT(
        ElevenLabsSTTConfig(
            api_key="k",
            mode="realtime",
            final_transcript_timeout_s=10.0,
        )
    )
    stt._ws = MockWebSocket([])
    stt._audio_pending_commit = True
    stt._audio_epoch = 1
    stt._partial_text = "best available transcript"

    commit_task = asyncio.create_task(stt._send_commit(wait_for_final=True))
    try:
        await asyncio.sleep(0)
        assert len(stt._pending_manual_commits) == 1

        await stt._on_reconnect()

        assert await asyncio.wait_for(commit_task, timeout=0.1) is True
        event = stt._event_queue.get_nowait()
        assert event.type is STTEventType.FINAL
        assert event.text == "best available transcript"
    finally:
        if not commit_task.done():
            commit_task.cancel()
        await asyncio.gather(commit_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_elevenlabs_reconnect_promotes_uncommitted_partial_at_socket_boundary():
    """A dropped socket cannot carry an old partial into the next audio epoch."""
    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))
    stt._audio_pending_commit = True
    stt._audio_epoch = 1
    stt._partial_text = "before reconnect"

    await stt._on_reconnect()

    event = stt._event_queue.get_nowait()
    assert event.type is STTEventType.FINAL
    assert event.text == "before reconnect"
    assert stt._partial_text == ""
    assert not stt._audio_pending_commit
    assert stt._committed_through_epoch == 1


@pytest.mark.asyncio
async def test_elevenlabs_reconnect_promotes_partial_for_lost_manual_commit():
    """A non-waiting manual commit also loses its final on a dropped socket."""
    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))
    stt._ws = MockWebSocket([])
    stt._audio_pending_commit = True
    stt._audio_epoch = 1
    stt._partial_text = "manual segment"

    assert await stt._send_commit(wait_for_final=False)
    assert stt._pending_manual_commits
    await stt._on_reconnect()

    event = stt._event_queue.get_nowait()
    assert event.type is STTEventType.FINAL
    assert event.text == "manual segment"
    assert stt._partial_text == ""


@pytest.mark.asyncio
async def test_elevenlabs_realtime_promotes_partial_on_commit_timeout(monkeypatch):
    # Server sends only a partial and never the committed transcript, so
    # the end-of-turn commit times out and the latest partial must be
    # promoted to a FINAL (mirroring OpenAIRealtimeSTT).
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([_el_transcript("hello wor", is_final=False)])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello wor"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_drops_late_final_after_timeout_promotion(monkeypatch):
    # After a commit timeout promotes the partial to FINAL, a late
    # committed transcript for the same turn must be dropped so the turn
    # does not get two FINAL events.
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    collected: list = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)

    # A partial arrives, then the commit times out and promotes it.
    stt._handle_json_message(json.loads(_el_transcript("hello wor", is_final=False)))
    assert await stt._send_commit(wait_for_final=True) is True

    # The real committed transcript shows up late — it must be dropped.
    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))

    await stt.end_stream()
    await collect_task
    events = collected

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello wor"


@pytest.mark.asyncio
async def test_elevenlabs_realtime_keeps_late_final_when_no_partial_promoted(monkeypatch):
    # The commit times out but no partial ever arrived, so nothing was
    # promoted to a FINAL.  A real committed transcript arriving afterwards
    # is the turn's only transcript and must NOT be dropped.
    monkeypatch.setattr(elevenlabs_provider, "_FINAL_TRANSCRIPT_TIMEOUT_S", 0.05)

    ws = _BlockingWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    collected: list = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)

    # No partial arrives; the commit times out without promoting anything.
    assert await stt._send_commit(wait_for_final=True) is True
    assert stt._dropping_pending_final is False

    # The committed transcript shows up late — it is the only transcript
    # for the turn and must be emitted, not dropped.
    stt._handle_json_message(json.loads(_el_transcript("hello world", is_final=True)))

    await stt.end_stream()
    await collect_task
    events = collected

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello world"


# ── version_info ─────────────────────────────────────────────────


def test_elevenlabs_version_info_sdk_matches_active_transport():
    """sdk_version reflects the transport the active mode uses."""
    from easycat._provider_helpers import get_package_version

    rt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="realtime"))
    assert rt.version_info()["sdk_version"] == get_package_version("websockets")

    batch = ElevenLabsSTT(ElevenLabsSTTConfig(api_key="k", mode="batch"))
    assert batch.version_info()["sdk_version"] == get_package_version("httpx")


# ── Batch mode ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_batch_transcribes():
    mock_client = _make_mock_http_client("batch result")
    config = ElevenLabsSTTConfig(api_key="test-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=200)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert len(events) == 1
    assert events[0].type == STTEventType.FINAL
    assert events[0].text == "batch result"


@pytest.mark.asyncio
async def test_elevenlabs_batch_sends_wav():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="test-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    call_kwargs = mock_client.post.call_args
    files = call_kwargs.kwargs.get("files", {})
    assert "file" in files
    _, data, _ = files["file"]
    assert data[:4] == b"RIFF"


@pytest.mark.asyncio
async def test_elevenlabs_batch_preserves_multichannel_non_pcm16_wav_geometry():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="test-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)
    stereo_pcm8 = AudioChunk(
        data=b"\x01\x02\x03\x04",
        format=AudioFormat(sample_rate=8000, channels=2, sample_width=1),
    )

    with pytest.raises(ValueError, match="PCM16"):
        await collect_stt_events(stt, [stereo_pcm8])


@pytest.mark.asyncio
async def test_elevenlabs_batch_sends_auth():
    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="xi-key-123", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    await collect_stt_events(stt, make_audio_chunks(pcm))

    headers = mock_client.post.call_args.kwargs.get("headers", {})
    assert headers["xi-api-key"] == "xi-key-123"


@pytest.mark.asyncio
async def test_elevenlabs_batch_no_event_on_empty():
    mock_client = _make_mock_http_client()
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    events = await collect_stt_events(stt, [])
    assert len(events) == 0


@pytest.mark.asyncio
async def test_elevenlabs_batch_rejects_mid_stream_format_change():
    from easycat.audio_format import AudioChunk, AudioFormat

    mock_client = _make_mock_http_client("test")
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    fmt_16k = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    fmt_8k = AudioFormat(sample_rate=8000, channels=1, sample_width=2)

    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00\x00" * 160, format=fmt_16k))
    with pytest.raises(ValueError, match="mid-stream audio format change"):
        await stt.send_audio(AudioChunk(data=b"\x00\x00" * 160, format=fmt_8k))


@pytest.mark.asyncio
async def test_elevenlabs_batch_rejects_oversized_audio_chunk_before_buffering():
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="batch",
        max_audio_chunk_bytes=4,
        max_audio_buffer_bytes=100,
        http_client=_make_mock_http_client("test"),
    )
    stt = ElevenLabsSTT(config)

    await stt.start_stream()
    with pytest.raises(ValueError, match="audio chunk exceeds"):
        await stt.send_audio(AudioChunk(data=b"\x00" * 6, format=PCM16_MONO_16K))

    assert len(stt._buffer) == 0


@pytest.mark.asyncio
async def test_elevenlabs_batch_finalizes_utterance_when_buffer_cap_hit():
    """A cumulative byte cap finalizes the current utterance, not an error."""
    mock_client = _make_mock_http_client("partial utterance")
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="batch",
        max_audio_chunk_bytes=10,
        max_audio_buffer_bytes=8,
        http_client=mock_client,
    )
    stt = ElevenLabsSTT(config)

    await stt.start_stream()
    await stt.send_audio(AudioChunk(data=b"\x00" * 4, format=PCM16_MONO_16K))
    # Total would be 4 + 6 = 10 > cap of 8: finalize the buffered 4 bytes and
    # restart with the 6-byte chunk. No exception is raised.
    await stt.send_audio(AudioChunk(data=b"\x00" * 6, format=PCM16_MONO_16K))

    # The buffered audio so far was transcribed (one request) and the new
    # chunk now occupies a fresh buffer.
    mock_client.post.assert_called_once()
    assert len(stt._buffer) == 6


@pytest.mark.asyncio
async def test_elevenlabs_batch_rejects_nonpositive_byte_rate_for_duration_cap():
    """A corrupted format must raise a clear error, not divide by zero."""
    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="batch",
        max_audio_duration_ms=1000,
        http_client=_make_mock_http_client("test"),
    )
    stt = ElevenLabsSTT(config)
    bad_format = object.__new__(AudioFormat)
    object.__setattr__(bad_format, "sample_rate", 0)
    object.__setattr__(bad_format, "channels", 1)
    object.__setattr__(bad_format, "sample_width", 2)
    object.__setattr__(bad_format, "encoding", "pcm")

    await stt.start_stream()
    with pytest.raises(ValueError, match="non-positive byte rate"):
        await stt.send_audio(AudioChunk(data=b"\x00" * 4, format=bad_format))

    assert len(stt._buffer) == 0


@pytest.mark.asyncio
async def test_elevenlabs_batch_with_confidence():
    mock_client = _make_mock_http_client("test", confidence=0.88)
    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    assert events[0].confidence == 0.88


@pytest.mark.asyncio
async def test_elevenlabs_batch_error_handling():
    error_response = httpx.Response(
        status_code=401,
        json={"error": "Unauthorized"},
        request=httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(return_value=error_response)
    mock_client.aclose = AsyncMock()

    config = ElevenLabsSTTConfig(api_key="bad-key", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    await stt.start_stream()
    for c in chunks:
        await stt.send_audio(c)

    with pytest.raises(httpx.HTTPStatusError):
        await stt.end_stream()


@pytest.mark.asyncio
async def test_elevenlabs_batch_retries_on_transient_429(monkeypatch):
    """A transient 429 is retried and the recovered transcript survives."""
    # Neutralize the exponential backoff so the 2**0=1s sleep does not run.
    monkeypatch.setattr(base.asyncio, "sleep", AsyncMock())

    request = httpx.Request("POST", "https://api.elevenlabs.io/v1/speech-to-text")
    rate_limited = httpx.Response(status_code=429, text="rate limited", request=request)
    recovered = httpx.Response(status_code=200, json={"text": "recovered"}, request=request)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = AsyncMock(side_effect=[rate_limited, recovered])
    mock_client.aclose = AsyncMock()

    config = ElevenLabsSTTConfig(api_key="k", mode="batch", http_client=mock_client)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))

    finals = [e for e in events if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "recovered"
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_elevenlabs_realtime_promotes_partial_on_final_timeout_via_config_field():
    """The config field — not just the module constant — drives the wait."""
    ws = _BlockingWebSocket([])

    async def mock_connect(url, **kwargs):
        return ws

    config = ElevenLabsSTTConfig(
        api_key="k",
        mode="realtime",
        ws_connect=mock_connect,
        final_transcript_timeout_s=0.05,
    )
    stt = ElevenLabsSTT(config)

    collected: list = []
    await stt.start_stream()

    async def _collect() -> None:
        async for event in stt.events():
            collected.append(event)

    collect_task = asyncio.create_task(_collect())
    chunk = make_audio_chunks(generate_pcm_sine(duration_ms=100), chunk_duration_ms=100)[0]
    await stt.send_audio(chunk)

    # A partial arrives, then the commit times out (per the config field) and
    # promotes the partial to FINAL.
    stt._handle_json_message(json.loads(_el_transcript("hello wor", is_final=False)))
    assert await stt._send_commit(wait_for_final=True) is True

    await stt.end_stream()
    await collect_task

    finals = [e for e in collected if e.type == STTEventType.FINAL]
    assert len(finals) == 1
    assert finals[0].text == "hello wor"


# ── Mode property ────────────────────────────────────────────────


def test_elevenlabs_mode_property():
    config = ElevenLabsSTTConfig(api_key="k", mode="realtime")
    stt = ElevenLabsSTT(config)
    assert stt.mode == "realtime"

    config2 = ElevenLabsSTTConfig(api_key="k", mode="batch")
    stt2 = ElevenLabsSTT(config2)
    assert stt2.mode == "batch"


# ── Multiple streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elevenlabs_realtime_reusable():
    call_count = 0

    async def mock_connect(url, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockWebSocket([_el_transcript(f"stream {call_count}", is_final=True)])

    config = ElevenLabsSTTConfig(api_key="k", mode="realtime", ws_connect=mock_connect)
    stt = ElevenLabsSTT(config)

    pcm = generate_pcm_sine(duration_ms=100)
    chunks = make_audio_chunks(pcm)

    events1 = await collect_stt_events(stt, chunks)
    assert events1[0].text == "stream 1"

    events2 = await collect_stt_events(stt, chunks)
    assert events2[0].text == "stream 2"


# ── Live integration ─────────────────────────────────────────────


@pytest.mark.integration_live
@pytest.mark.provider_elevenlabs
@pytest.mark.surface_stt
async def test_live_elevenlabs_stt_realtime():
    """Integration test requiring ELEVENLABS_API_KEY env var."""
    import os

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        pytest.skip("ELEVENLABS_API_KEY not set")

    stt = ElevenLabsSTT(ElevenLabsSTTConfig(api_key=api_key, mode="realtime"))

    pcm = generate_pcm_sine(duration_ms=500, sample_rate=16000)
    events = await collect_stt_events(stt, make_audio_chunks(pcm))
    # Tone isn't real speech; smoke-gates auth + realtime WebSocket
    # session negotiation.
    assert isinstance(events, list)
