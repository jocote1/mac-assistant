"""MacAssistant's menu bar app, voice recording, and scheduled checks.

Runs the push-to-talk shortcut, request processing, inbox checks, and daily briefings.
"""

import os
import sys

if getattr(sys, "frozen", False) and "--self-check" not in sys.argv:
    from pathlib import Path
    _log_dir = Path.home() / ".mac_assistant"
    _log_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(_log_dir / "assistant.log", "a", buffering=1)

# Auto-re-execute inside local .venv if run with system/conda python
_project_dir = os.path.dirname(os.path.abspath(__file__))
_venv_dir = os.path.join(_project_dir, ".venv")
_venv_python = os.path.join(_venv_dir, "bin", "python")

if not getattr(sys, "frozen", False) and os.path.exists(_venv_python) and os.path.abspath(sys.prefix) != os.path.abspath(_venv_dir):
    os.execv(_venv_python, [_venv_python] + sys.argv)

import io
import queue
import subprocess
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from pathlib import Path

try:
    import numpy as np
    import requests
    import rumps
    import sounddevice as sd
    from pynput import keyboard

    import macos_bridge
    from global_hotkey import GlobalHotkey
    from ai_pipeline import AIPipeline
    from config import AppConfig, AppState, LOG_FILE, open_settings_window
    from tts_engine import get_tts_engine
except ModuleNotFoundError as e:
    print(f"\n[Error] Missing required dependency: '{e.name}'.", file=sys.stderr)
    print("To activate the virtual environment and run the assistant, use:", file=sys.stderr)
    print("    source .venv/bin/activate", file=sys.stderr)
    print("    python main.py\n", file=sys.stderr)
    print("Or run directly with the virtual environment interpreter:", file=sys.stderr)
    print("    .venv/bin/python main.py\n", file=sys.stderr)
    sys.exit(1)


ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def update_ui(app, *, status=None, state=None, record_title=None, background=False):
    """Marshal worker updates to the main run loop; voice owns UI while active."""
    values = (status, state, record_title, background)
    pending = getattr(app, "_ui_queue", None)
    if pending is not None:
        pending.put(values)
    else:
        apply_ui(app, values)


def apply_ui(app, values):
    status, state, record_title, background = values
    if background and (getattr(app, "is_recording", False) or getattr(app, "is_processing", False)):
        return
    if status is not None:
        app.menu_status.title = status
    if state is not None:
        app.title = ""
        app.icon = str(ASSETS_DIR / f"{state}.png")
    if record_title is not None:
        app.menu_toggle_record.title = record_title
        if hasattr(app.menu_toggle_record, "set_icon"):
            glyph = "stop" if getattr(app, "is_recording", False) else "recording"
            app.menu_toggle_record.set_icon(str(ASSETS_DIR / f"{glyph}.png"), dimensions=(16, 16), template=True)


class AudioRecorder:
    """Handles in-memory 16kHz mono PCM audio recording."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.recording = False
        self.stream: Optional[sd.InputStream] = None
        self.frames: List[np.ndarray] = []
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Audio] Status: {status}", file=sys.stderr)
        if self.recording:
            with self._lock:
                self.frames.append(indata.copy())

    def start(self):
        with self._lock:
            self.frames = []
            self.recording = True
        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                callback=self._callback,
            )
            self.stream.start()
        except Exception as e:
            print(f"[Audio] Error starting input stream: {e}", file=sys.stderr)
            self.recording = False
            if self.stream:
                self.stream.close()
                self.stream = None
            raise RuntimeError("Microphone unavailable. Check Microphone access in System Settings.") from e

    def stop(self) -> bytes:
        with self._lock:
            self.recording = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

        with self._lock:
            if not self.frames:
                return b""
            audio_data = np.concatenate(self.frames, axis=0)

        # Convert to in-memory WAV
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_data.tobytes())

        return bio.getvalue()


class MacAssistantApp(rumps.App):
    """Native macOS Status Bar Application."""

    def __init__(self):
        super().__init__("MacAssistant", title="", icon=str(ASSETS_DIR / "idle.png"), template=True)
        from AppKit import NSApplication, NSImage
        NSApplication.sharedApplication().setApplicationIconImage_(
            NSImage.alloc().initByReferencingFile_(str(ASSETS_DIR / "AppIcon.png")))

        self.config = AppConfig()
        self.state = AppState()
        self.pipeline = AIPipeline(self.config)
        self.tts = get_tts_engine(self.config)
        self.recorder = AudioRecorder(sample_rate=16000)

        self.is_recording = False
        self.is_processing = False
        self._voice_generation = 0
        self._voice_lock = threading.Lock()
        self._ptt_held = False
        self._email_check_lock = threading.Lock()
        self._last_unread_snapshot = None
        self._hotkey_listener: Optional[keyboard.Listener] = None
        self._ui_queue = queue.Queue()
        self._briefing_locks = {"morning": threading.Lock(), "evening": threading.Lock()}
        self._briefing_retry_after = {"morning": 0.0, "evening": 0.0}

        # Build Menu
        tts_status_str = "ON" if self.config.tts_enabled else "OFF"
        self.menu_status = rumps.MenuItem("Status: Idle", callback=None)
        self.menu_hotkey_status = rumps.MenuItem("Hotkey: checking permissions...", callback=None)
        self.menu_toggle_record = rumps.MenuItem("Record Voice Query (PTT)", callback=self.on_toggle_record_click)
        self.menu_morning = rumps.MenuItem("Run Morning Briefing Now", callback=self.on_run_morning_briefing)
        self.menu_evening = rumps.MenuItem("Run Evening Wrap-Up Now", callback=self.on_run_evening_wrapup)
        self.menu_triage = rumps.MenuItem("Check Unread Emails Now", callback=self.on_check_emails_now)
        self.menu_tts = rumps.MenuItem(f"Toggle Spoken Audio (TTS: {tts_status_str})", callback=self.on_toggle_tts)
        self.menu_logs = rumps.MenuItem("View Logs...", callback=self.on_view_logs)
        self.menu_pref = rumps.MenuItem("Preferences / Settings...", callback=self.on_preferences)
        for item, glyph in [(self.menu_toggle_record, "recording"), (self.menu_morning, "calendar"),
                            (self.menu_evening, "moon"), (self.menu_triage, "mail"),
                            (self.menu_tts, "speaker"), (self.menu_logs, "logs"), (self.menu_pref, "settings")]:
            item.set_icon(str(ASSETS_DIR / f"{glyph}.png"), dimensions=(16, 16), template=True)

        self.menu = [
            self.menu_status,
            self.menu_hotkey_status,
            None,
            self.menu_toggle_record,
            self.menu_morning,
            self.menu_evening,
            self.menu_triage,
            None,
            self.menu_tts,
            self.menu_logs,
            self.menu_pref,
            None,
        ]

        # Start Global Hotkey Listener
        self._init_hotkey_listener()
        self._ui_timer = rumps.Timer(self._flush_ui, 0.1)
        self._ui_timer.start()
        self._settings_timer = rumps.Timer(self._refresh_settings, 2)
        self._settings_timer.start()

        # Start Background Scheduler Thread (Daemon)
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def _flush_ui(self, _):
        while True:
            try:
                values = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            apply_ui(self, values)

    def _refresh_settings(self, _):
        # Shortcut registration and menu updates must run on the main run loop.
        if self.is_recording or self.is_processing:
            return
        old_hotkey = self.config.ptt_hotkey
        old_tts = self.config.tts_enabled
        old_mail = (self.config.get("mail_provider"), self.config.get("mail_email"))
        if not self.config.reload():
            return
        self.pipeline.refresh_config()
        if old_mail != (self.config.get("mail_provider"), self.config.get("mail_email")):
            self._last_unread_snapshot = None
        if old_tts and not self.config.tts_enabled:
            self.tts.stop()
        status = "ON" if self.config.tts_enabled else "OFF"
        self.menu_tts.title = f"Toggle Spoken Audio (TTS: {status})"
        if old_hotkey != self.config.ptt_hotkey:
            if getattr(self, "_native_hotkey", None):
                self._native_hotkey.stop()
                self._native_hotkey = None
            if self._hotkey_listener:
                self._hotkey_listener.stop()
                self._hotkey_listener = None
            self._ptt_held = False
            self._init_hotkey_listener()

    # -----------------------------------------------------------------
    # Push-to-Talk (PTT) Global Key Handling
    # -----------------------------------------------------------------

    def _is_cmd_key(self, key) -> bool:
        return key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r) or getattr(key, "name", "") in ("cmd", "cmd_l", "cmd_r")

    def _is_key_5(self, key) -> bool:
        return getattr(key, "char", None) == "5" or getattr(key, "vk", None) in (23, 87)

    def _matches_hotkey(self, key) -> bool:
        """Check whether a single key matches configured hotkeys (for single-key mode)."""
        target = self.config.ptt_hotkey.lower().strip()

        if target in ("alt_r", "right_option", "option_r"):
            return key == keyboard.Key.alt_r or (hasattr(key, "name") and key.name == "alt_r")

        if target in ("cmd_r", "right_command", "command_r"):
            return key == keyboard.Key.cmd_r or (hasattr(key, "name") and key.name == "cmd_r")

        if target == "f6":
            return key == keyboard.Key.f6

        if target in ("ctrl+space", "space"):
            return key == keyboard.Key.space

        return False

    def _init_hotkey_listener(self):
        """Start pynput global keyboard listener for Push-to-Talk."""
        target = self.config.ptt_hotkey.lower().strip()
        if target in GlobalHotkey.SHORTCUTS:
            def pressed():
                if not self._ptt_held:
                    self._ptt_held = True
                    self.start_recording()
            def released():
                if self._ptt_held:
                    self._ptt_held = False
                    self.stop_recording_and_process()
            try:
                self._native_hotkey = GlobalHotkey(target, pressed, released)
                self._native_hotkey.start()
                self.menu_hotkey_status.title = f"Hotkey: {target} (native)"
                print(f"[PTT] Native global shortcut active on '{target}' (no keyboard-monitoring permission required).", file=sys.stderr)
            except Exception as error:
                self.menu_hotkey_status.title = "Hotkey unavailable: shortcut conflict"
                print(f"[PTT] {error}", file=sys.stderr)
                macos_bridge.display_notification("Shortcut Unavailable", str(error))
            return
        pressed_keys = set()
        combo_active = False

        def on_press(key):
            nonlocal combo_active
            pressed_keys.add(key)
            target = self.config.ptt_hotkey.lower().strip()

            if target in ("cmd+5", "cmd5", "command+5"):
                has_cmd = any(self._is_cmd_key(k) for k in pressed_keys)
                has_5 = any(self._is_key_5(k) for k in pressed_keys)
                if has_cmd and has_5:
                    if not self._ptt_held:
                        self._ptt_held = True
                        combo_active = True
                        self.start_recording()
            else:
                if self._matches_hotkey(key):
                    if not self._ptt_held:
                        self._ptt_held = True
                        self.start_recording()

        def on_release(key):
            nonlocal combo_active
            target = self.config.ptt_hotkey.lower().strip()

            if target in ("cmd+5", "cmd5", "command+5"):
                if self._ptt_held and combo_active:
                    # Release when either Command or 5 is released
                    if self._is_cmd_key(key) or self._is_key_5(key):
                        self._ptt_held = False
                        combo_active = False
                        self.stop_recording_and_process()
            else:
                if self._matches_hotkey(key):
                    if self._ptt_held:
                        self._ptt_held = False
                        self.stop_recording_and_process()

            pressed_keys.discard(key)

        try:
            import HIServices
            if not HIServices.AXIsProcessTrusted():
                self.menu_hotkey_status.title = "Hotkey unavailable: allow Accessibility"
                print("[PTT] Cmd+5 is unavailable: enable Terminal/Python in System Settings > Privacy & Security > Accessibility, then restart. Use the assistant menu's Record Voice Query meanwhile.", file=sys.stderr)
                macos_bridge.display_notification("Hotkey Permission Needed", "Enable Accessibility for Terminal/Python. You can record from the assistant menu now.")
                return
            self._hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            self.menu_hotkey_status.title = f"Hotkey: {self.config.ptt_hotkey}"
            print(f"[PTT] Global hotkey listener active on '{self.config.ptt_hotkey}'.", file=sys.stderr)
        except Exception as e:
            print(f"[PTT] Could not start global keyboard listener: {e}", file=sys.stderr)
            print("[PTT] Note: macOS requires Accessibility permission for global hotkeys.", file=sys.stderr)
            macos_bridge.display_notification(
                "Accessibility Permission Needed",
                "Grant Terminal / Python Accessibility permission for global PTT hotkey, or use menu bar recording."
            )

    # -----------------------------------------------------------------
    # Recording & Pipeline Processing
    # -----------------------------------------------------------------

    def start_recording(self):
        """Begin audio capture and show the microphone glyph."""
        if self.is_recording:
            return
        self._voice_generation += 1
        self.tts.pause()
        try:
            self.recorder.start()
        except Exception as error:
            self._ptt_held = False
            self.tts.resume()
            update_ui(self, status="Microphone unavailable", state="idle")
            macos_bridge.display_notification("Microphone Unavailable", str(error))
            return
        self.is_recording = True
        update_ui(self, status="Listening…", state="recording", record_title="Stop Recording & Process")
        print("[Voice] Started recording...", file=sys.stderr)

    def stop_recording_and_process(self):
        """Stop capture, show the busy glyph, and process the request in a worker."""
        if not self.is_recording:
            return
        self.is_recording = False
        self.is_processing = True
        update_ui(self, status="Thinking…", state="busy", record_title="Record Voice Query (PTT)")

        wav_bytes = self.recorder.stop()
        self.tts.resume()
        voice_generation = self._voice_generation
        speech_generation = self.tts.generation

        def worker():
            try:
                # Check for minimum audio length (~0.35s = ~11,200 bytes)
                if len(wav_bytes) < 12000:
                    print("[Voice] Audio buffer too short, ignoring click.", file=sys.stderr)
                    return

                # Check API Key
                if not self.pipeline.is_configured():
                    msg = "Please set your OpenAI API key in Preferences."
                    macos_bridge.display_notification("API Key Required", msg)
                    self.tts.speak(msg, generation=speech_generation)
                    return

                # 1. Transcribe STT
                print("[Voice] Transcribing speech via OpenAI...", file=sys.stderr)
                transcription = self.pipeline.transcribe_audio_wav(wav_bytes)
                if voice_generation != self._voice_generation:
                    return
                print(f"[Voice] Transcription: '{transcription}'", file=sys.stderr)

                if not transcription.strip():
                    return

                update_ui(self, status=f"Heard: {transcription[:40]}")

                # 2. Execute Classify -> Template Fill -> Native Execution
                print("[Voice] Executing slot-filling and OS action...", file=sys.stderr)
                with self._voice_lock:
                    if voice_generation != self._voice_generation:
                        return
                    result = self.pipeline.execute_pipeline(transcription)
                if voice_generation != self._voice_generation:
                    return

                spoken_text = result.get("spoken_response", "Action completed.")
                print(f"[Voice] Assistant Spoken Response: '{spoken_text}'", file=sys.stderr)

                # 3. Speak response via Kokoro-82M / macOS say
                update_ui(self, status="Speaking…")
                self.tts.speak(spoken_text, block=True, generation=speech_generation)

            except Exception as e:
                err_msg = f"Voice processing error: {e}"
                print(f"[Voice] {err_msg}", file=sys.stderr)
                macos_bridge.display_notification("Assistant Error", str(e))
                self.tts.speak("Sorry, I encountered an issue processing that.", generation=speech_generation)
            finally:
                if voice_generation == self._voice_generation:
                    self.is_processing = False
                    update_ui(self, status="Ready", state="idle")

        threading.Thread(target=worker, daemon=True).start()

    def on_toggle_record_click(self, _):
        """Menu click handler to manually toggle recording on/off."""
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording_and_process()

    # -----------------------------------------------------------------
    # Automations: 30-Minute Zero-Token Email Triage Loop
    # -----------------------------------------------------------------

    def run_email_triage(self, manual: bool = False):
        """Summarize every unread message; speak changed inboxes and all manual checks."""
        if not self.pipeline.is_configured():
            if manual:
                macos_bridge.display_notification("API Key Required", "Set your API key in Preferences to summarize the inbox.")
            return
        if not manual and (getattr(self, "is_recording", False) or getattr(self, "is_processing", False)):
            return
        if not self._email_check_lock.acquire(blocking=False):
            print("[Inbox] An unread email check is already running.", file=sys.stderr)
            return

        def worker():
            speech_generation = self.tts.generation
            try:
                update_ui(self, status="Checking inbox…", background=True)
                print("[Inbox] Checking all unread emails (including previously processed messages).", file=sys.stderr)
                emails = macos_bridge.get_unread_emails(limit=50)
                snapshot = tuple(sorted((str(e.get("id", "")), e.get("sender", ""), e.get("subject", "")) for e in emails))
                print(f"[Inbox] Found {len(emails)} unread emails.", file=sys.stderr)
                if not manual and snapshot == self._last_unread_snapshot:
                    return
                update_ui(self, status="Checking inbox…", background=True)
                emails, trashed = self.pipeline.cleanup_inbox(emails)
                print(f"[Inbox] Cleanup: {trashed} moved to Trash, {len(emails)} kept.", file=sys.stderr)
                snapshot = tuple(sorted((str(e.get("id", "")), e.get("sender", ""), e.get("subject", "")) for e in emails))
                if not manual and snapshot == self._last_unread_snapshot:
                    print("[Inbox] Unread inbox unchanged; next check in the configured interval.", file=sys.stderr)
                    return

                update_ui(self, status="Summarizing inbox…", background=True)
                spoken = self.pipeline.summarize_emails(emails)
                print(f"[Inbox] Summary: {spoken}", file=sys.stderr)
                macos_bridge.display_notification("Unread Email Summary", spoken)
                # Only authorized disposable-email cleanup runs automatically.
                self.tts.speak(spoken, block=True, generation=speech_generation)
                self._last_unread_snapshot = snapshot
            except Exception as e:
                print(f"[Inbox] Email summary failed: {e}", file=sys.stderr)
                macos_bridge.display_notification("Email Summary Error", str(e))
            finally:
                self.state.record_email_check()
                update_ui(self, status="Ready", background=True)
                self._email_check_lock.release()
                print("[App] Ready in the macOS menu bar (assistant icon).", file=sys.stderr)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            self._email_check_lock.release()
            raise

    def on_check_emails_now(self, _):
        self.run_email_triage(manual=True)

    # -----------------------------------------------------------------
    # Automations: Context-Aware Morning Briefing & Evening Wrap-Up
    # -----------------------------------------------------------------

    def run_morning_briefing(self):
        """Read a morning briefing from weather, calendar, reminders, and mail."""
        if not self.pipeline.is_configured():
            return
        if self.is_recording or self.is_processing:
            return
        if time.monotonic() < self._briefing_retry_after["morning"]:
            return
        lock = self._briefing_locks["morning"]
        if not lock.acquire(blocking=False):
            return
        def worker():
            speech_generation = self.tts.generation
            try:
                update_ui(self, state="busy", background=True)
                update_ui(self, status="Preparing morning briefing…", background=True)
                print("[Briefing] Generating Morning Briefing...", file=sys.stderr)

                # 1. Weather
                weather = "Unavailable; do not infer current weather."
                try:
                    resp = requests.get("https://wttr.in/?format=3", timeout=4)
                    if resp.status_code == 200 and resp.text.strip():
                        weather = resp.text.strip()
                except Exception:
                    pass

                # 2. Today's Calendar Schedule
                now = datetime.now().astimezone()
                start_today = now.replace(hour=0, minute=0, second=0)
                end_today = now.replace(hour=23, minute=59, second=59)
                events = macos_bridge.get_calendar_events(start_today, end_today)

                # 3. Due today & overdue reminders
                reminders = macos_bridge.get_reminders(include_completed=False, due_before=end_today)

                # 4. Urgent unread emails
                unread = macos_bridge.get_unread_emails(limit=10)
                urgent = [e for e in unread if macos_bridge.is_actionable_email(e["sender"], e["subject"], e["snippet"])]

                # 5. Generate briefing
                briefing_text = self.pipeline.generate_morning_briefing(
                    weather=weather,
                    events=events,
                    reminders=reminders,
                    emails=urgent,
                )

                print(f"[Briefing] Spoken briefing: '{briefing_text}'", file=sys.stderr)
                self.state.record_morning_briefing()

                macos_bridge.display_notification(
                    "Good Morning!",
                    f"{len(events)} events today, {len(reminders)} open tasks.",
                    subtitle="Morning briefing"
                )

                update_ui(self, status="Speaking…", background=True)
                self.tts.speak(briefing_text, block=True, generation=speech_generation)

            except Exception as e:
                self._briefing_retry_after["morning"] = time.monotonic() + 300
                print(f"[Briefing] Morning briefing error: {e}", file=sys.stderr)
                macos_bridge.display_notification("Briefing Error", str(e))
            finally:
                update_ui(self, status="Ready", state="idle", background=True)
                lock.release()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            lock.release()
            raise

    def on_run_morning_briefing(self, _):
        self.run_morning_briefing()

    def run_evening_wrapup(self):
        """Audits tasks and previews tomorrow's schedule."""
        if not self.pipeline.is_configured():
            return
        if self.is_recording or self.is_processing:
            return
        if time.monotonic() < self._briefing_retry_after["evening"]:
            return
        lock = self._briefing_locks["evening"]
        if not lock.acquire(blocking=False):
            return
        def worker():
            speech_generation = self.tts.generation
            try:
                update_ui(self, state="busy", background=True)
                update_ui(self, status="Preparing evening wrap-up…", background=True)

                # Reminders
                reminders = macos_bridge.get_reminders(include_completed=False)

                # Tomorrow's schedule
                now = datetime.now().astimezone()
                tomorrow = now + timedelta(days=1)
                start_tomorrow = tomorrow.replace(hour=0, minute=0, second=0)
                end_tomorrow = tomorrow.replace(hour=23, minute=59, second=59)
                tomorrow_events = macos_bridge.get_calendar_events(start_tomorrow, end_tomorrow)
                tomorrow_events.sort(key=lambda event: event.get("start_iso", ""))
                first_event = tomorrow_events[0] if tomorrow_events else None

                wrapup_text = self.pipeline.generate_evening_wrapup(
                    completed_tasks=None,
                    remaining_tasks=reminders[:5],
                    tomorrow_event=first_event,
                )

                print(f"[WrapUp] Spoken wrap-up: '{wrapup_text}'", file=sys.stderr)
                self.state.record_evening_wrapup()

                macos_bridge.display_notification(
                    "Evening Wrap-Up",
                    f"{len(reminders)} open tasks remaining.",
                    subtitle="Daily Wrap-Up"
                )

                update_ui(self, status="Speaking…", background=True)
                self.tts.speak(wrapup_text, block=True, generation=speech_generation)

            except Exception as e:
                self._briefing_retry_after["evening"] = time.monotonic() + 300
                print(f"[WrapUp] Evening wrapup error: {e}", file=sys.stderr)
            finally:
                update_ui(self, status="Ready", state="idle", background=True)
                lock.release()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception:
            lock.release()
            raise

    def on_run_evening_wrapup(self, _):
        self.run_evening_wrapup()

    # -----------------------------------------------------------------
    # Background Scheduler Thread
    # -----------------------------------------------------------------

    def _scheduler_loop(self):
        """Run inbox checks and daily briefings at the configured times."""
        last_triage_time = 0.0

        while not self._scheduler_stop.is_set():
            now = datetime.now()
            now_ts = time.time()
            today_str = now.strftime("%Y-%m-%d")
            current_hhmm = now.strftime("%H:%M")

            # 1. 30-Minute Email Triage Loop
            poll_interval_sec = self.config.poll_interval_minutes * 60
            if now_ts - last_triage_time >= poll_interval_sec and not (self.is_recording or self.is_processing):
                last_triage_time = now_ts
                self.run_email_triage(manual=False)

            # 2. Morning Briefing Trigger (first wake/login between configured time and noon)
            cfg_morning = self.config.morning_briefing_time
            if current_hhmm >= cfg_morning and now.hour < 12:
                if self.state.last_morning_briefing_date != today_str:
                    print(f"[Scheduler] Triggering automatic Morning Briefing for {today_str}.", file=sys.stderr)
                    self.run_morning_briefing()

            # 3. Evening Wrap-Up Trigger (at configured hour, e.g., 20:00)
            cfg_evening = self.config.evening_wrapup_time
            if current_hhmm >= cfg_evening:
                if self.state.last_evening_wrapup_date != today_str:
                    print(f"[Scheduler] Triggering automatic Evening Wrap-Up for {today_str}.", file=sys.stderr)
                    self.run_evening_wrapup()

            # Sleep 30 seconds between scheduler checks
            self._scheduler_stop.wait(30)

    # -----------------------------------------------------------------
    # Preferences & TTS Toggles
    # -----------------------------------------------------------------

    def on_toggle_tts(self, _):
        new_val = not self.config.tts_enabled
        self.config.set("tts_enabled", new_val)
        if not new_val:
            self.tts.stop()
        status_str = "ON" if new_val else "OFF"
        self.menu_tts.title = f"Toggle Spoken Audio (TTS: {status_str})"
        macos_bridge.display_notification("TTS Audio", f"Spoken audio is now {status_str}.")

    def on_preferences(self, _):
        """Open the Tkinter Preferences GUI in a separate subprocess."""
        open_settings_window()

    def on_view_logs(self, _):
        """Open the current application log in macOS Console."""
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOG_FILE.touch(exist_ok=True)
            subprocess.Popen(["/usr/bin/open", "-a", "Console", str(LOG_FILE)])
        except Exception as error:
            message = f"Could not open the log: {error}"
            print(f"[App] {message}", file=sys.stderr)
            macos_bridge.display_notification("Could Not Open Logs", message)


def main():
    """Application Entry Point."""
    print("=" * 60)
    print("Starting MacAssistant...")
    print("=" * 60)
    if "--settings" in sys.argv:
        from config import launch_settings_gui
        launch_settings_gui()
        return
    if "--self-check" in sys.argv:
        import kokoro_onnx, onnxruntime, espeakng_loader
        import EventKit, Security
        import tkinter
        from tts_engine import get_tts_engine
        engine = get_tts_engine()
        print(f"[App] SELF-CHECK OK: bundled imports loaded; Kokoro warm={engine.is_warm()}")
        return
    # Keep login launches and repeated Finder opens from running duplicate assistants.
    import fcntl
    from config import ASSISTANT_DIR
    ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
    instance_lock = open(ASSISTANT_DIR / "app.lock", "a")
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[App] Assistant is already running in the menu bar.")
        return
    app = MacAssistantApp()
    print("[App] Running in the macOS menu bar. Click the assistant icon to record or check emails. Ctrl+C in this terminal quits.", file=sys.stderr)
    app.run()


if __name__ == "__main__":
    main()
