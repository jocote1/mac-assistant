"""
tts_engine.py - Local Text-to-Speech Engine with warm Kokoro-82M pipeline and macOS say fallback.

Features:
- Warm-loaded Kokoro-82M pipeline via kokoro-onnx on Apple Silicon.
- Automatic, seamless fallback to macOS native 'say' CLI if model weights are missing.
- In-memory playback via sounddevice with stop/interrupt support.
- Configurable voices (e.g., af_heart, am_michael) and speech speed.
"""

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

# Official voice files are NumPy archives and load without enabling pickle globally.

from config import MODELS_DIR, AppConfig

KOKORO_MODEL_FILE = MODELS_DIR / "kokoro-v0_19.onnx"
KOKORO_VOICES_FILE = MODELS_DIR / "voices-v1.0.bin"
KOKORO_VOICES_FALLBACK_FILE = MODELS_DIR / "voices.bin"

# Download URLs for official Kokoro-82M ONNX release
KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"


class KokoroTTSEngine:
    """
    Manages local TTS synthesis with warm Kokoro-82M pipeline and macOS say fallback.
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()
        self.kokoro = None
        self._is_warm = False
        self._current_process: Optional[subprocess.Popen] = None
        self._playback_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._generation = 0
        self._paused = False
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread = None

        self._init_engine()
        self._start_worker()

    def _init_engine(self):
        """Warm up Kokoro-82M pipeline if weights are present; otherwise log fallback notice."""
        voices_path = None
        if KOKORO_VOICES_FILE.exists() and KOKORO_VOICES_FILE.stat().st_size > 1000:
            voices_path = KOKORO_VOICES_FILE
        elif KOKORO_VOICES_FALLBACK_FILE.exists() and KOKORO_VOICES_FALLBACK_FILE.stat().st_size > 1000:
            voices_path = KOKORO_VOICES_FALLBACK_FILE

        if KOKORO_MODEL_FILE.exists() and voices_path:
            try:
                import kokoro_onnx
                print(f"[TTS] Loading Kokoro-82M ONNX model ({KOKORO_MODEL_FILE.name}) and voices into memory...", file=sys.stderr)
                self.kokoro = kokoro_onnx.Kokoro(str(KOKORO_MODEL_FILE), str(voices_path))
                available_voices = self.kokoro.get_voices()
                # Ensure configured voice is available, fallback to first if not
                test_voice = self.config.tts_voice if self.config.tts_voice in available_voices else (available_voices[0] if available_voices else "af_heart")
                # Warm-up silent synthesis for near-instant first response
                _ = self.kokoro.create(".", voice=test_voice, speed=1.0)
                self._is_warm = True
                print(f"[TTS] Kokoro-82M pipeline is warm in memory ({len(available_voices)} voices available, default: '{test_voice}').", file=sys.stderr)
            except Exception as e:
                print(f"[TTS] Failed to warm-load Kokoro: {e}. Falling back to macOS 'say'.", file=sys.stderr)
                self.kokoro = None
                self._is_warm = False
        else:
            print(
                f"[TTS] Kokoro weights not found at {MODELS_DIR}. "
                "Using native macOS 'say' CLI fallback (Download Kokoro weights anytime via settings).",
                file=sys.stderr
            )
            self.kokoro = None
            self._is_warm = False

    def is_warm(self) -> bool:
        return self._is_warm

    @property
    def generation(self):
        with self._state_lock:
            return self._generation

    def _start_worker(self):
        def worker():
            while not self._stop_event.is_set():
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    self._queue.task_done()
                    return
                text, voice, speed, generation, done = item
                try:
                    self._play_sync(text, voice, speed, generation)
                except Exception as e:
                    print(f"[TTS] Worker playback error: {e}", file=sys.stderr)
                finally:
                    done.set()
                    self._queue.task_done()
        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def speak(self, text: str, voice: Optional[str] = None, speed: Optional[float] = None,
              block: bool = False, generation: Optional[int] = None):
        if not self.config.tts_enabled or not text.strip():
            return
        done = threading.Event()
        with self._state_lock:
            generation = self._generation if generation is None else generation
            if self._paused or generation != self._generation:
                return
            self._queue.put((text.strip(), voice or self.config.tts_voice,
                             speed if speed is not None else self.config.tts_speed,
                             generation, done))
        if block:
            done.wait()

    def _cancelled(self, generation):
        return self._paused or generation != self._generation or not self.config.tts_enabled

    def _play_sync(self, text: str, voice: str, speed: float, generation: int):
        with self._playback_lock:
            with self._state_lock:
                if self._cancelled(generation):
                    return
            if self.kokoro and self._is_warm:
                try:
                    import sounddevice as sd
                    available = self.kokoro.get_voices()
                    active_voice = voice if voice in available else (available[0] if available else "af_heart")
                    samples, rate = self.kokoro.create(text, voice=active_voice, speed=speed, lang="en-us")
                    # A hotkey press during synthesis invalidates this audio before it starts.
                    with self._state_lock:
                        if self._cancelled(generation):
                            return
                        print(f"[TTS] Speaking via Kokoro-82M ({active_voice}, duration: {len(samples) / rate:.1f}s)...", file=sys.stderr)
                        sd.play(samples, rate)
                    sd.wait()
                    return
                except Exception as e:
                    with self._state_lock:
                        if self._cancelled(generation):
                            return
                    print(f"[TTS] Kokoro playback failed ({e}), falling back to macOS 'say'.", file=sys.stderr)
            process = None
            try:
                voice_args = ["-v", "Daniel"] if any(v in voice for v in ("michael", "adam", "george")) else ["-v", "Samantha"]
                with self._state_lock:
                    if self._cancelled(generation):
                        return
                    print("[TTS] Speaking via macOS 'say'.", file=sys.stderr)
                    rate = str(round(175 * max(0.8, min(1.4, speed))))
                    process = subprocess.Popen(["say", *voice_args, "-r", rate, "--", text])
                    self._current_process = process
                process.wait()
            finally:
                with self._state_lock:
                    if self._current_process is process:
                        self._current_process = None

    def pause(self):
        """Cancel speech and suppress new playback while the microphone is recording."""
        with self._state_lock:
            self._paused = True
            self.stop()

    def resume(self):
        with self._state_lock:
            if self._paused:
                # Discard background results that began while the microphone was open.
                self._generation += 1
            self._paused = False

    def stop(self):
        """Cancel playing, queued, and currently synthesizing speech."""
        with self._state_lock:
            self._generation += 1
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[-1].set()
                self._queue.task_done()
            try:
                import sounddevice as sd
                sd.stop()
            except Exception as e:
                print(f"[TTS] Audio stop error: {e}", file=sys.stderr)
            if self._current_process and self._current_process.poll() is None:
                self._current_process.terminate()
            print("[TTS] Speech interrupted.", file=sys.stderr)


def download_kokoro_weights(progress_callback: Optional[Callable[[str, float], None]] = None) -> bool:
    """
    Download Kokoro-82M ONNX model and voices file to ~/.mac_assistant/models/.
    """
    import urllib.request

    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    def _download_file(url: str, dest: Path, label: str):
        if dest.exists() and dest.stat().st_size > 1000:
            if progress_callback:
                progress_callback(f"{label} already downloaded.", 1.0)
            return True

        if progress_callback:
            progress_callback(f"Downloading {label}...", 0.0)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MacAssistant/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response, open(dest, "wb") as out_file:
                total_length = response.headers.get("content-length")
                total_bytes = int(total_length) if total_length else 0
                downloaded = 0
                chunk_size = 64 * 1024

                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes > 0 and progress_callback:
                        pct = downloaded / total_bytes
                        progress_callback(f"Downloading {label} ({int(pct*100)}%)...", pct)

            if progress_callback:
                progress_callback(f"{label} downloaded successfully.", 1.0)
            return True
        except Exception as e:
            print(f"[TTS] Download error for {url}: {e}", file=sys.stderr)
            if dest.exists():
                dest.unlink(missing_ok=True)
            return False

    success_model = _download_file(KOKORO_MODEL_URL, KOKORO_MODEL_FILE, "Kokoro-82M Model (~310MB)")
    success_voices = _download_file(KOKORO_VOICES_URL, KOKORO_VOICES_FILE, "Kokoro Voices (~28MB)")

    return success_model and success_voices


# Global TTS singleton
_tts_engine: Optional[KokoroTTSEngine] = None


def get_tts_engine(config: Optional[AppConfig] = None) -> KokoroTTSEngine:
    global _tts_engine
    if _tts_engine is None:
        _tts_engine = KokoroTTSEngine(config)
    return _tts_engine
