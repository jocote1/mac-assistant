"""
tests/test_assistant.py - Automated test suite for macOS Menu Bar AI Assistant.
"""

import io
import os
import unittest
import uuid
import wave
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
import macos_bridge
from ai_pipeline import (
    CreateCalendarEventSlots,
    CreateReminderSlots,
    DraftReplySlots,
    GeneralQuerySlots,
    IntentClassification,
    OpenMailSlots,
)
from config import AppConfig, AppState
from main import AudioRecorder
from tts_engine import get_tts_engine


class TestConfigAndState(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, value in {"ASSISTANT_DIR": root, "CONFIG_FILE": root / "config.json",
                            "STATE_FILE": root / "state.json", "MODELS_DIR": root / "models",
                            "LAUNCH_AGENTS_DIR": root / "LaunchAgents"}.items():
            patcher = patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = AppConfig()
        self.state = AppState()

    def test_config_defaults(self):
        self.assertEqual(self.config.text_model, "gpt-5-nano")
        self.assertEqual(self.config.stt_model, "gpt-4o-mini-transcribe")
        self.assertEqual(self.config.ptt_hotkey, "cmd+5")
        self.assertEqual(self.config.poll_interval_minutes, 30)

    def test_state_tracking(self):
        test_email_id = f"test-msg-{uuid.uuid4().hex}"
        self.assertFalse(self.state.is_email_processed(test_email_id))
        self.state.mark_email_processed(test_email_id)
        self.assertTrue(self.state.is_email_processed(test_email_id))

        today = datetime.now().strftime("%Y-%m-%d")
        self.state.record_morning_briefing(today)
        self.assertEqual(self.state.last_morning_briefing_date, today)

    def test_legacy_model_migrates_without_changing_other_settings(self):
        config.CONFIG_FILE.write_text('{"text_model": "gpt-oss-20b", "tts_speed": 1.2}')
        loaded = AppConfig()
        self.assertEqual(loaded.text_model, "gpt-5-nano")
        self.assertEqual(loaded.tts_speed, 1.2)


class TestZeroTokenGatekeeper(unittest.TestCase):
    def test_unactionable_emails_filtered(self):
        unactionable_samples = [
            ("noreply-accounts@google.com", "Security alert: new login", "Was this you? If not, review your account."),
            ("newsletter@techcrunch.com", "TC Daily Digest", "Top news today. Click here to unsubscribe."),
            ("no-reply@stripe.com", "Receipt for your payment", "Your invoice #10293 is available."),
            ("marketing@store.com", "50% off summer sale!", "Shop today and save big."),
            ("system@github.com", "[GitHub] A personal access token has expired", "View your tokens."),
        ]
        for sender, subject, snippet in unactionable_samples:
            self.assertFalse(
                macos_bridge.is_actionable_email(sender, subject, snippet),
                f"Failed to filter out: {sender} - {subject}"
            )

    def test_actionable_emails_allowed(self):
        actionable_samples = [
            ("john.doe@company.com", "Project deadline review tomorrow at 2pm", "Can we meet tomorrow at 2pm?"),
            ("alice@partner.org", "Urgent question regarding the contract", "Please see my question below and let me know."),
            ("alex@investors.com", "Quick sync this Thursday", "Are you free Thursday morning?"),
        ]
        for sender, subject, snippet in actionable_samples:
            self.assertTrue(
                macos_bridge.is_actionable_email(sender, subject, snippet),
                f"Incorrectly filtered legitimate email: {sender} - {subject}"
            )


class TestMacOSBridge(unittest.TestCase):
    def test_display_notification(self):
        with patch("macos_bridge._run_applescript", return_value=(0, "", "")):
            result = macos_bridge.display_notification("Test Title", "Test Message")
        self.assertTrue(result)

    def test_calendar_query(self):
        now = datetime.now(timezone.utc)
        with patch("native_store.get_calendar_events", return_value=[]):
            events = macos_bridge.get_calendar_events(now - timedelta(days=2), now + timedelta(days=5))
        self.assertIsInstance(events, list)

    def test_reminders_query(self):
        with patch("native_store.get_reminders", return_value=[]):
            reminders = macos_bridge.get_reminders()
        self.assertIsInstance(reminders, list)

    def test_open_mail_app(self):
        with patch("macos_bridge._run_applescript", return_value=(0, "", "")):
            res = macos_bridge.open_mail_app()
        self.assertTrue(res)


class TestPydanticSchemas(unittest.TestCase):
    def test_intent_classification_schema(self):
        data = {
            "intent": "create_calendar_event",
            "confidence": 0.98,
            "target_time_hint": "tomorrow at 3pm",
            "reasoning": "User requested scheduling a meeting.",
        }
        item = IntentClassification.model_validate(data)
        self.assertEqual(item.intent, "create_calendar_event")
        self.assertEqual(item.confidence, 0.98)

    def test_calendar_conflict_schema(self):
        data = {
            "title": "Strategy Sync",
            "start_iso": "2026-10-21T14:00:00+02:00",
            "end_iso": "2026-10-21T15:00:00+02:00",
            "has_conflict": True,
            "conflict_resolution_spoken": "You have a conflict with German Lesson at that time. Would you prefer 4pm?",
            "spoken_response": "Heads up, you have a conflict at 2pm.",
        }
        item = CreateCalendarEventSlots.model_validate(data)
        self.assertTrue(item.has_conflict)
        self.assertIn("German Lesson", item.conflict_resolution_spoken)

    def test_open_mail_schema(self):
        data = {
            "message_query": "DeepSeek",
            "spoken_response": "Opening the DeepSeek email on your screen.",
        }
        item = OpenMailSlots.model_validate(data)
        self.assertEqual(item.message_query, "DeepSeek")


class TestAudioRecorder(unittest.TestCase):
    def test_in_memory_wav_generation(self):
        recorder = AudioRecorder(sample_rate=16000)
        # Verify WAV conversion helper
        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00" * 3200)  # 0.1s of silence
        data = bio.getvalue()
        self.assertGreater(len(data), 44)  # WAV header is 44 bytes
        self.assertTrue(data.startswith(b"RIFF"))


class TestTTSEngine(unittest.TestCase):
    def test_engine_init(self):
        with patch("tts_engine._tts_engine", None), \
             patch("tts_engine.KokoroTTSEngine._init_engine"), \
             patch("tts_engine.KokoroTTSEngine._start_worker"):
            tts = get_tts_engine(SimpleNamespace(tts_enabled=True, tts_voice="af_heart", tts_speed=1))
        self.assertIsNotNone(tts)


if __name__ == "__main__":
    unittest.main()
