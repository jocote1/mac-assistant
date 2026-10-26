"""Safety and interruption regressions using mocked audio, Mail and Decisions."""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_pipeline import AIPipeline
from main import MacAssistantApp
from macos_bridge import trash_email_by_id
from tts_engine import KokoroTTSEngine


def engine_without_hardware():
    engine = KokoroTTSEngine.__new__(KokoroTTSEngine)
    engine.config = SimpleNamespace(tts_enabled=True, tts_voice="af_heart", tts_speed=1)
    engine._state_lock = threading.RLock()
    engine._playback_lock = threading.Lock()
    engine._generation = 0
    engine._paused = False
    engine._queue = queue.Queue()
    engine._stop_event = threading.Event()
    engine._current_process = None
    engine.kokoro = Mock()
    engine.kokoro.get_voices.return_value = ["af_heart"]
    engine._is_warm = True
    return engine


class InterruptionTests(unittest.TestCase):
    def test_interrupt_during_synthesis_prevents_late_playback(self):
        engine = engine_without_hardware()
        started, release = threading.Event(), threading.Event()
        def synthesize(*args, **kwargs):
            started.set()
            release.wait(2)
            return [0.1] * 10, 100
        engine.kokoro.create.side_effect = synthesize
        audio = Mock()
        with patch.dict("sys.modules", {"sounddevice": audio}):
            worker = threading.Thread(target=engine._play_sync, args=("Long old response", "af_heart", 1, 0))
            worker.start()
            self.assertTrue(started.wait(1))
            engine.pause()
            engine.resume()  # Old speech must remain invalid even after key release.
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            audio.stop.assert_called_once()
            audio.play.assert_not_called()

    def test_pause_clears_queue_blocks_new_speech_and_stale_responses(self):
        engine = engine_without_hardware()
        engine.speak("queued")
        with patch.dict("sys.modules", {"sounddevice": Mock()}):
            engine.pause()
        self.assertTrue(engine._queue.empty())
        engine.speak("generated while recording")
        self.assertTrue(engine._queue.empty())
        engine.resume()
        engine.speak("old background result", generation=0)
        self.assertTrue(engine._queue.empty())
        engine.speak("new response")
        self.assertEqual(engine._queue.qsize(), 1)

    def test_hotkey_recording_interrupts_even_while_processing(self):
        app = SimpleNamespace(is_recording=False, is_processing=True,
            _voice_generation=4, tts=Mock(), recorder=Mock(),
            menu_status=SimpleNamespace(title=""), menu_toggle_record=SimpleNamespace(title=""))
        MacAssistantApp.start_recording(app)
        app.tts.pause.assert_called_once()
        app.recorder.start.assert_called_once()
        self.assertTrue(app.is_recording)
        self.assertEqual(app._voice_generation, 5)

    def test_stop_terminates_native_say(self):
        engine = engine_without_hardware()
        process = Mock()
        process.poll.return_value = None
        engine._current_process = process
        with patch.dict("sys.modules", {"sounddevice": Mock()}):
            engine.stop()
        process.terminate.assert_called_once()

    def test_playback_interrupt_releases_blocking_speak(self):
        engine = engine_without_hardware()
        playing, stopped = threading.Event(), threading.Event()
        audio = Mock()
        audio.play.side_effect = lambda *args: playing.set()
        audio.wait.side_effect = lambda: stopped.wait(2)
        audio.stop.side_effect = stopped.set
        engine.kokoro.create.return_value = ([0.1] * 10, 100)
        with patch.dict("sys.modules", {"sounddevice": audio}):
            engine._start_worker()
            caller = threading.Thread(target=lambda: engine.speak("Response", block=True))
            caller.start()
            try:
                self.assertTrue(playing.wait(1))
                engine.pause()
                caller.join(1)
                self.assertFalse(caller.is_alive())
            finally:
                stopped.set()
                engine._queue.put(None)
                engine._worker_thread.join(2)

    def test_background_result_started_during_recording_is_discarded(self):
        engine = engine_without_hardware()
        with patch.dict("sys.modules", {"sounddevice": Mock()}):
            engine.pause()
        during_recording = engine.generation
        engine.resume()
        engine.speak("Background email summary", generation=during_recording)
        self.assertTrue(engine._queue.empty())


class CleanupTests(unittest.TestCase):
    def pipeline(self):
        pipeline = AIPipeline.__new__(AIPipeline)
        pipeline._inbox_cleanup_lock = threading.Lock()
        pipeline._cleanup_decisions = {}
        return pipeline

    def test_uses_decisions_choice_endpoint(self):
        pipeline = self.pipeline()
        client = Mock()
        pipeline.get_client = Mock(return_value=client)
        endpoint = client.with_options.return_value.decisions.create
        endpoint.return_value = SimpleNamespace(answers=[SimpleNamespace(name="email_cleanup", type="choice", choice="obvious_ad", confidence=0.99)])
        self.assertEqual(pipeline.classify_email_cleanup({"id": "42", "subject": "Sale", "content": "Buy now"}), ("obvious_ad", 0.99))
        self.assertEqual(endpoint.call_args.kwargs["model"], "gpt-6-luna")
        self.assertEqual(endpoint.call_args.kwargs["questions"][0]["type"], "choice")
        client.beta.chat.completions.parse.assert_not_called()

    def test_only_confident_disposable_messages_are_trashed_by_id(self):
        pipeline = self.pipeline()
        pipeline.classify_email_cleanup = Mock(side_effect=[("obvious_ad", 0.99), ("temporary_verification", 0.98), ("keep", 0.99), ("obvious_ad", 0.6)])
        emails = [{"id": str(i), "subject": "Same sender", "content": "body"} for i in range(4)]
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            remaining, count = pipeline.cleanup_inbox(emails)
        self.assertEqual([call.args[0] for call in trash.call_args_list], ["0", "1"])
        self.assertEqual([e["id"] for e in remaining], ["2", "3"])
        self.assertEqual(count, 2)

    def test_refusal_and_endpoint_failure_keep_email(self):
        pipeline = self.pipeline()
        client = Mock()
        pipeline.get_client = Mock(return_value=client)
        endpoint = client.with_options.return_value.decisions.create
        endpoint.return_value = SimpleNamespace(answers=[SimpleNamespace(name="email_cleanup", type="refusal")])
        self.assertEqual(pipeline.classify_email_cleanup({}), ("keep", 0))
        endpoint.side_effect = RuntimeError("not available")
        with patch("ai_pipeline.macos_bridge.trash_email_by_id") as trash:
            remaining, count = pipeline.cleanup_inbox([{"id": "42", "content": "body"}])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(count, 0)
        trash.assert_not_called()

    def test_failed_delete_stays_in_summary(self):
        pipeline = self.pipeline()
        pipeline.classify_email_cleanup = Mock(return_value=("obvious_ad", 0.99))
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": False}):
            remaining, count = pipeline.cleanup_inbox([{"id": "42", "content": "body"}])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(count, 0)

    def test_bridge_reports_invalid_imap_reference_without_launching_mail(self):
        with patch("imap_mail.trash_email_by_id", side_effect=ValueError("Stale email reference")) as move, \
             patch("macos_bridge._run_applescript") as script:
            self.assertFalse(trash_email_by_id('42; delete everything')["success"])
            move.assert_called_once_with('42; delete everything')
            script.assert_not_called()


if __name__ == "__main__":
    unittest.main()
