"""Microphone capture for voice messages.

The agent's model (Gemini) understands audio directly: a spoken request travels to it as an
audio Part, exactly the way a typed request travels as text, and the model transcribes and
acts on it in the same turn. There is therefore no speech-to-text step here - this module
does one thing, record the microphone into a WAV blob, and hands that blob to the turn.

Recording is push-to-talk: `start()` opens the input stream, `stop()` closes it and returns
the audio. It runs off the SDK's own callback thread, so the interface stays responsive
while the user speaks; the frames are just accumulated and packed into a WAV container at
the end. 16 kHz mono is the sweet spot for speech - intelligible to the model, and a
fraction of the size (so a fraction of the upload) of CD-quality audio.

sounddevice is an optional dependency: if it or a microphone is missing, `is_available()`
says so and the interface simply does not offer the button, rather than failing to start.
"""

import io
import threading
import wave

SAMPLE_RATE = 16000  # enough for speech; keeps the upload small
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample (int16)
MIME_TYPE = "audio/wav"


def is_available():
    """True when the microphone can actually be used - the library is installed and a
    capture device exists. Checked before the button is offered, so a machine without a mic
    never shows a control that could only fail."""
    try:
        import sounddevice as sd

        sd.query_devices(kind="input")
        return True
    except Exception:  # noqa: BLE001 - a missing library, driver or device all mean "no"
        return False


def to_wav(raw, samplerate=SAMPLE_RATE):
    """Wraps raw little-endian int16 PCM in a WAV container the model will accept."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH)
        writer.setframerate(samplerate)
        writer.writeframes(raw)
    return buffer.getvalue()


class Recorder:
    """A single push-to-talk recording. Reusable: start/stop as many times as needed."""

    def __init__(self, samplerate=SAMPLE_RATE):
        self.samplerate = samplerate
        self._stream = None
        self._frames = []
        self._lock = threading.Lock()

    @property
    def recording(self):
        return self._stream is not None

    @property
    def duration(self):
        """Seconds captured so far, from the byte count - so the button can show a timer."""
        with self._lock:
            captured = sum(len(frame) for frame in self._frames)
        return captured / (self.samplerate * SAMPLE_WIDTH * CHANNELS)

    def start(self):
        """Opens the input stream and begins accumulating audio. A no-op if already running."""
        if self._stream is not None:
            return
        import sounddevice as sd

        self._frames = []

        def callback(indata, _frames, _time, _status):
            # RawInputStream hands raw bytes (a cffi buffer); copy them out of the SDK's
            # buffer, which it reuses for the next block.
            with self._lock:
                self._frames.append(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self.samplerate,
            channels=CHANNELS,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()

    def stop(self):
        """Stops recording and returns the captured audio as WAV bytes (b"" if nothing ran)."""
        if self._stream is None:
            return b""
        self._stream.stop()
        self._stream.close()
        self._stream = None
        with self._lock:
            raw = b"".join(self._frames)
            self._frames = []
        return to_wav(raw, self.samplerate)

    def cancel(self):
        """Discards an in-progress recording without producing audio."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            self._frames = []
