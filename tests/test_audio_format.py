from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, AudioChunk, AudioFormat


def test_audio_format_frame_size():
    fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
    assert fmt.frame_size == 2

    stereo = AudioFormat(sample_rate=16000, channels=2, sample_width=2)
    assert stereo.frame_size == 4


def test_audio_format_bytes_per_second():
    assert PCM16_MONO_8K.bytes_per_second == 16000
    assert PCM16_MONO_16K.bytes_per_second == 32000


def test_pcm16_mono_constants():
    assert PCM16_MONO_8K.sample_rate == 8000
    assert PCM16_MONO_8K.channels == 1
    assert PCM16_MONO_8K.sample_width == 2
    assert PCM16_MONO_8K.encoding == "pcm"

    assert PCM16_MONO_16K.sample_rate == 16000
    assert PCM16_MONO_16K.channels == 1
    assert PCM16_MONO_16K.sample_width == 2


def test_audio_chunk_num_samples():
    # 320 bytes at 16kHz mono 16-bit = 160 samples = 10ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    assert chunk.num_samples == 160


def test_audio_chunk_duration_ms():
    # 160 samples at 16kHz = 10ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_16K)
    assert chunk.duration_ms == 10.0

    # 160 samples at 8kHz = 20ms
    data = bytes(320)
    chunk = AudioChunk(data=data, format=PCM16_MONO_8K)
    assert chunk.duration_ms == 20.0


def test_audio_chunk_has_timestamp():
    chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
    assert isinstance(chunk.timestamp, float)
    assert chunk.timestamp > 0
