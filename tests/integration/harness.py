"""Reusable helpers for end-to-end integration tests."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Iterable, Sequence
from typing import Any

from easycat import EasyCatConfig
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import STTEvent, STTEventType, TTSEvent, TTSEventType
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.tts.input import TTSInput, coerce_tts_input
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig

FAST_TURN_CONFIG = TurnManagerConfig(end_of_turn_silence_ms=1)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_chunk(n_bytes: int = 640) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


async def wait_for_condition(
    predicate: Any,
    *,
    timeout: float = 2.0,
    interval: float = 0.01,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met before timeout")


class EventCollector:
    def __init__(self, event_bus: Any) -> None:
        self._event_bus = event_bus
        self.events: list[Any] = []
        self._changed = asyncio.Event()

    def subscribe(self, *event_types: type[Any]) -> None:
        for event_type in event_types:
            self._event_bus.subscribe(event_type, self._record)

    def _record(self, event: Any) -> None:
        self.events.append(event)
        self._changed.set()

    async def wait_for(
        self,
        event_type: type[Any],
        *,
        predicate: Any | None = None,
        timeout: float = 2.0,
    ) -> Any:
        predicate = predicate or (lambda _event: True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        index = 0

        while True:
            for event in self.events[index:]:
                if isinstance(event, event_type) and predicate(event):
                    return event
            index = len(self.events)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for {event_type.__name__}")
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), timeout=remaining)


class QueueTransport:
    def __init__(
        self,
        *,
        fail_on_send: Exception | None = None,
        fail_after_n_sends: int | None = None,
    ) -> None:
        self._incoming: asyncio.Queue[AudioChunk | None] = asyncio.Queue()
        self.sent: list[AudioChunk] = []
        self.clear_calls = 0
        self.connected = False
        self.disconnected = False
        self._fail_on_send = fail_on_send
        self._fail_after_n_sends = fail_after_n_sends
        self._send_count = 0

    async def connect(self) -> None:
        self.connected = True
        self.disconnected = False

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True
        await self._incoming.put(None)

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        while True:
            chunk = await self._incoming.get()
            if chunk is None:
                break
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._fail_on_send is not None:
            if self._fail_after_n_sends is None or self._send_count >= self._fail_after_n_sends:
                self._send_count += 1
                raise self._fail_on_send
        self._send_count += 1
        self.sent.append(chunk)

    async def clear_audio(self) -> None:
        self.clear_calls += 1

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "test",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }

    async def push_audio(self, *chunks: AudioChunk) -> None:
        for chunk in chunks:
            await self._incoming.put(chunk)

    async def finish_input(self) -> None:
        await self._incoming.put(None)


class ScriptedVAD:
    def __init__(
        self,
        script: Sequence[str | Sequence[str]],
        *,
        fail_on_chunk: int | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._script = list(script)
        self._index = 0
        self._chunk_count = 0
        self._fail_on_chunk = fail_on_chunk
        self._fail_with = fail_with or RuntimeError("scripted VAD failure")

    async def process(self, chunk: AudioChunk) -> AsyncIterator[Any]:
        current_chunk = self._chunk_count
        self._chunk_count += 1

        if self._fail_on_chunk is not None and current_chunk == self._fail_on_chunk:
            raise self._fail_with

        if self._index >= len(self._script):
            return

        step = self._script[self._index]
        self._index += 1
        actions = step if isinstance(step, Sequence) and not isinstance(step, str) else [step]

        for action in actions:
            if action == "start":
                from easycat.events import VADStartSpeaking

                yield VADStartSpeaking()
            elif action == "stop":
                from easycat.events import VADStopSpeaking

                yield VADStopSpeaking()

    def configure(self, **kwargs: object) -> None:
        del kwargs


class ScriptedSTT:
    def __init__(
        self,
        transcripts: Iterable[str],
        *,
        fail_on_start: Exception | None = None,
        partials: Sequence[Sequence[str]] | None = None,
    ) -> None:
        self._transcripts = list(transcripts)
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self.sent_audio: list[AudioChunk] = []
        self.start_calls = 0
        self.end_calls = 0
        self._fail_on_start = fail_on_start
        self._partials = list(partials) if partials else None

    async def start_stream(self) -> None:
        self.start_calls += 1
        if self._fail_on_start is not None:
            raise self._fail_on_start
        self._events = asyncio.Queue()

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_audio.append(chunk)

    async def end_stream(self) -> None:
        self.end_calls += 1
        turn_idx = self.end_calls - 1
        if self._partials and turn_idx < len(self._partials):
            for partial_text in self._partials[turn_idx]:
                await self._events.put(STTEvent(type=STTEventType.PARTIAL, text=partial_text))
        transcript = self._transcripts.pop(0) if self._transcripts else ""
        if transcript:
            await self._events.put(STTEvent(type=STTEventType.FINAL, text=transcript))
        await self._events.put(None)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event


class RecordingTTS:
    supports_ssml = False

    def __init__(
        self,
        *,
        chunk_sizes: Sequence[int] = (640,),
        chunk_delay_s: float = 0.0,
        fail_on_synthesize: Exception | None = None,
        fail_on_cancel: Exception | None = None,
    ) -> None:
        self.chunk_sizes = tuple(chunk_sizes)
        self.chunk_delay_s = chunk_delay_s
        self.payloads: list[TTSInput] = []
        self._fail_on_synthesize = fail_on_synthesize
        self._fail_on_cancel = fail_on_cancel

    async def synthesize(self, payload: TTSInput | str) -> AsyncIterator[TTSEvent]:
        coerced = coerce_tts_input(payload)
        self.payloads.append(coerced)
        if self._fail_on_synthesize is not None:
            raise self._fail_on_synthesize
        for size in self.chunk_sizes:
            if self.chunk_delay_s > 0:
                await asyncio.sleep(self.chunk_delay_s)
            yield TTSEvent(type=TTSEventType.AUDIO, audio=make_chunk(size))

    async def stop(self) -> None:
        return None

    async def cancel(self) -> None:
        if self._fail_on_cancel is not None:
            raise self._fail_on_cancel


class IdentityNoiseReducer:
    async def process(self, chunk: AudioChunk) -> AudioChunk:
        return chunk


class FailingNoiseReducer:
    def __init__(self, fail_on_chunk: int = 0) -> None:
        self._fail_on_chunk = fail_on_chunk
        self._call_count = 0

    async def process(self, chunk: AudioChunk) -> AudioChunk:
        if self._call_count == self._fail_on_chunk:
            self._call_count += 1
            raise RuntimeError("noise reduction failed")
        self._call_count += 1
        return chunk


class QueuePlaybackTransport(QueueTransport):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.playback_marks: list[str] = []
        self._mark_seq = 0

    async def send_playback_mark(self, name: str | None = None) -> str:
        self._mark_seq += 1
        mark_name = name or f"mark_{self._mark_seq}"
        self.playback_marks.append(mark_name)
        return mark_name


def patch_provider_factories(
    monkeypatch: Any,
    *,
    stt: Any,
    tts: Any,
    vad: Any,
    noise_reducer: Any | None = None,
    echo_canceller: Any | None = None,
) -> None:
    import easycat.config as config_module

    monkeypatch.setattr(
        config_module,
        "create_stt_provider_from_config",
        lambda config, event_bus: stt,
    )
    monkeypatch.setattr(
        config_module,
        "create_tts_provider_from_config",
        lambda config, event_bus: tts,
    )
    monkeypatch.setattr(config_module, "create_vad", lambda config: vad)
    monkeypatch.setattr(
        config_module,
        "create_noise_reducer",
        lambda config: noise_reducer or IdentityNoiseReducer(),
    )
    monkeypatch.setattr(
        config_module,
        "create_echo_canceller",
        lambda config: echo_canceller or PassthroughAEC(),
    )


def make_test_config(
    *,
    transport: Any,
    agent: Any,
    telephony: Any | None = None,
    turn_taking: TurnManagerConfig | None = None,
) -> EasyCatConfig:
    return EasyCatConfig(
        stt=OpenAISTTConfig(api_key="test-key"),
        tts=OpenAITTSConfig(api_key="test-key"),
        transport=transport,
        agent=agent,
        telephony=telephony,
        turn_taking=turn_taking or FAST_TURN_CONFIG,
    )


def twilio_start_message(stream_sid: str = "MZ123", call_sid: str = "CA456") -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": stream_sid,
            "start": {"streamSid": stream_sid, "callSid": call_sid},
        }
    )


def twilio_media_message(payload_b64: str, *, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload_b64},
        }
    )


def twilio_dtmf_message(digit: str, *, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "dtmf", "streamSid": stream_sid, "dtmf": {"digit": digit}})


def twilio_mark_message(name: str, *, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


def twilio_stop_message(*, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})
