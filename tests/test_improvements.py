"""Conversation, cost, native outcome and UI regressions. No paid API or app mutations."""
import json
import queue
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import openai

import config
import macos_bridge
from ai_pipeline import (
    AIPipeline, ActionPlan, PlannedAction, SpokenReplySlots, EmailSummarySlots,
    CreateCalendarEventSlots, CreateReminderSlots, DraftReplySlots, OpenMailSlots,
    ReadMailSlots,
)
from main import AudioRecorder, MacAssistantApp, apply_ui, update_ui, ASSETS_DIR
from test_interrupt_cleanup import engine_without_hardware


def pipeline():
    cfg = config.AppConfig.__new__(config.AppConfig)
    cfg.config = dict(config.DEFAULT_CONFIG)
    with patch.object(AIPipeline, "_init_client"):
        return AIPipeline(cfg)


def completion(parsed):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed, refusal=None))])


def api_error(kind, message, status):
    return kind(message, response=SimpleNamespace(status_code=status, request=Mock(), headers={}), body=None)


class ConversationTests(unittest.TestCase):
    def test_conversation_uses_one_call_and_remembers_followup(self):
        assistant = pipeline()
        assistant._call_structured_llm = Mock(side_effect=[
            ActionPlan(actions=[PlannedAction(intent="general_query", request="capital of Austria", reply="Vienna.")]),
            ActionPlan(actions=[PlannedAction(intent="general_query", request="more about Vienna", reply="It sits on the Danube.")]),
        ])
        self.assertEqual(assistant.execute_pipeline("What's Austria's capital?")["spoken_response"], "Vienna.")
        assistant.execute_pipeline("Tell me more about it")
        self.assertEqual(assistant._call_structured_llm.call_count, 2)
        messages = assistant._call_structured_llm.call_args.args[0]
        self.assertIn({"role": "assistant", "content": "Vienna."}, messages)

    def test_history_budget_preserves_complete_recent_pairs(self):
        assistant = pipeline()
        assistant.conversation_history = [
            {"role": role, "content": str(index) * 1200}
            for index in range(5) for role in ("user", "assistant")
        ]
        history = assistant._history(3000)
        self.assertEqual(len(history), 2)
        self.assertEqual([entry["role"] for entry in history], ["user", "assistant"])
        self.assertEqual(history[-1]["content"], "4" * 1200)

    def test_general_fallback_includes_chat_context(self):
        assistant = pipeline()
        assistant.conversation_history = [{"role": "user", "content": "I live in Bergen."},
                                          {"role": "assistant", "content": "Got it."}]
        assistant._call_structured_llm = Mock(return_value=SpokenReplySlots(spoken_response="Bergen."))
        assistant._handle_general_query("Where do I live?", "")
        self.assertIn(assistant.conversation_history[0], assistant._call_structured_llm.call_args.args[0])

    def test_idle_timeout_clears_stale_email_references(self):
        assistant = pipeline()
        assistant.last_discussed_emails = [{"id": "old"}]
        assistant._call_structured_llm = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="general_query", request="hello", reply="Hi.")]))
        assistant.execute_pipeline("hello")
        self.assertEqual(assistant.last_discussed_emails, [])

    def test_reading_first_email_preserves_second_email_reference(self):
        assistant = pipeline()
        assistant.last_discussed_emails = [
            {"id": str(index), "sender": "Sam", "subject": str(index), "content": "Hello"}
            for index in (1, 2)
        ]
        assistant._call_structured_llm = Mock(return_value=ReadMailSlots(spoken_response=""))
        assistant._handle_read_mail("read the first email", "")
        result = assistant._handle_read_mail("read the second email", "")
        self.assertEqual(result["emails"][0]["id"], "2")

    def test_multiple_writes_share_confirmation_and_retain_errors(self):
        results = [
            {"intent": "create_reminder", "executed": True, "slots": {"title": title}, "spoken_response": f"Added {title}."}
            for title in ("Milk", "Bread")
        ] + [{"intent": "open_mail", "executed": False, "spoken_response": "Mail couldn't open."}]
        self.assertEqual(AIPipeline._combine_responses(results), "Added reminders: Milk, Bread. Mail couldn't open.")

    def test_failure_does_not_lose_other_requested_actions(self):
        assistant = pipeline()
        assistant.classify_action_plan = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="open_mail", request="open mail"),
            PlannedAction(intent="create_reminder", request="remind me to buy milk"),
        ]))
        assistant._execute_action = Mock(side_effect=[RuntimeError("Mail unavailable"),
            {"intent": "create_reminder", "executed": True, "spoken_response": "Added Milk."}])
        result = assistant.execute_pipeline("open mail and remind me to buy milk")
        self.assertFalse(result["executed"])
        self.assertIn("Mail unavailable", result["spoken_response"])
        self.assertIn("Added Milk", result["spoken_response"])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.assistant = pipeline()
        self.assistant.config.config["text_model"] = "gpt-4o-mini"
        self.client = Mock()
        self.assistant.get_client = Mock(return_value=self.client)
        self.parse = self.client.beta.chat.completions.parse
        self.parse.return_value = completion(SpokenReplySlots(spoken_response="Hello."))

    def test_request_has_output_limit_without_mutating_callers_prompt(self):
        messages = [{"role": "system", "content": "Answer."}, {"role": "user", "content": "Hello"}]
        self.assistant._call_structured_llm(messages, SpokenReplySlots)
        self.assertEqual(messages[0]["content"], "Answer.")
        self.assertEqual(self.parse.call_args.kwargs["max_completion_tokens"], 768)

    def test_refusal_cannot_be_treated_as_success(self):
        self.parse.return_value = completion(None)
        with self.assertRaisesRegex(ValueError, "usable response"):
            self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_count, 1)

    def test_authentication_failure_never_changes_model_or_retries(self):
        self.assistant.config.config["text_model"] = "gpt-5-nano"
        self.parse.side_effect = api_error(openai.AuthenticationError, "Invalid key", 401)
        with self.assertRaises(openai.AuthenticationError):
            self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_count, 1)

    def test_unsupported_reasoning_parameter_is_remembered(self):
        self.assistant.config.config["text_model"] = "gpt-5-nano"
        self.parse.side_effect = [api_error(openai.BadRequestError, "Unsupported reasoning_effort", 400),
                                 completion(SpokenReplySlots(spoken_response="Hi.")),
                                 completion(SpokenReplySlots(spoken_response="Hi."))]
        self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_count, 3)
        self.assertNotIn("reasoning_effort", self.parse.call_args.kwargs)

    def test_unavailable_model_fallback_is_remembered(self):
        self.assistant.config.config["text_model"] = "unavailable-model"
        self.parse.side_effect = [api_error(openai.NotFoundError, "Model not found", 404),
                                 completion(SpokenReplySlots(spoken_response="Hi.")),
                                 completion(SpokenReplySlots(spoken_response="Hi."))]
        self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_count, 3)
        self.assertEqual(self.parse.call_args.kwargs["model"], "gpt-5-nano")
        self.assertEqual(self.parse.call_args.kwargs["reasoning_effort"], "minimal")

    def test_nano_uses_minimal_reasoning_on_first_request(self):
        self.assistant.config.config["text_model"] = "gpt-5-nano"
        self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_args.kwargs["model"], "gpt-5-nano")
        self.assertEqual(self.parse.call_args.kwargs["reasoning_effort"], "minimal")
        self.assertEqual(self.parse.call_args.kwargs["max_completion_tokens"], 2048)
        self.assertEqual(self.parse.call_count, 1)

    def test_unavailable_nano_does_not_retry_same_model(self):
        self.assistant.config.config["text_model"] = "gpt-5-nano"
        self.parse.side_effect = api_error(openai.NotFoundError, "Model not found", 404)
        with self.assertRaises(openai.NotFoundError):
            self.assistant._call_structured_llm([], SpokenReplySlots)
        self.assertEqual(self.parse.call_count, 1)


class NativeOutcomeTests(unittest.TestCase):
    def test_failed_writes_never_announce_success(self):
        cases = [
            ("_handle_create_reminder", CreateReminderSlots(title="Milk", spoken_response="Done!"), "create_reminder"),
            ("_handle_draft_reply", DraftReplySlots(recipient_email_or_name="Sam", subject="Hi", reply_body="Hello", spoken_response="Done!"), "create_draft_reply"),
            ("_handle_open_mail", OpenMailSlots(spoken_response="Done!"), "open_mail_app"),
        ]
        for handler, slots, native in cases:
            with self.subTest(handler=handler):
                assistant = pipeline()
                assistant._call_structured_llm = Mock(return_value=slots)
                failure = False if native == "open_mail_app" else {"success": False, "error": "Permission denied"}
                with patch(f"ai_pipeline.macos_bridge.{native}", return_value=failure), \
                     patch("ai_pipeline.macos_bridge.display_notification") as notify:
                    result = getattr(assistant, handler)("do it", "")
                self.assertFalse(result["executed"])
                self.assertIn("couldn't", result["spoken_response"])
                self.assertNotIn("Done!", result["spoken_response"])
                notify.assert_not_called()

    def test_calendar_conflicts_use_target_date_and_native_truth(self):
        assistant = pipeline()
        slots = CreateCalendarEventSlots(title="Planning", start_iso="2027-05-03T10:00:00+02:00",
            end_iso="2027-05-03T11:00:00+02:00", has_conflict=True, spoken_response="Created!")
        assistant._call_structured_llm = Mock(return_value=slots)
        with patch("ai_pipeline.macos_bridge.get_calendar_events") as prefetch, \
             patch("ai_pipeline.macos_bridge.get_calendar_conflicts", return_value=([], [])) as conflicts, \
             patch("ai_pipeline.macos_bridge.create_calendar_event", return_value={"success": False, "error": "Calendar denied"}), \
             patch("ai_pipeline.macos_bridge.display_notification") as notify:
            result = assistant._handle_create_calendar_event("next May", "", datetime.now().astimezone())
        prefetch.assert_not_called()
        conflicts.assert_called_once_with(slots.start_iso, slots.end_iso)
        self.assertFalse(result["executed"])
        self.assertIn("Calendar denied", result["spoken_response"])
        notify.assert_not_called()

    def test_invalid_calendar_range_never_reaches_native_write(self):
        assistant = pipeline()
        assistant._call_structured_llm = Mock(return_value=CreateCalendarEventSlots(title="Bad", start_iso="2027-05-03T12:00:00",
            end_iso="2027-05-03T11:00:00", has_conflict=False, spoken_response="Done"))
        with patch("ai_pipeline.macos_bridge.create_calendar_event") as create:
            result = assistant._handle_create_calendar_event("bad range", "", datetime.now().astimezone())
        self.assertFalse(result["executed"])
        create.assert_not_called()

    def test_success_banners_are_opt_in(self):
        assistant = pipeline()
        assistant._call_structured_llm = Mock(return_value=CreateReminderSlots(title="Milk", spoken_response="Done"))
        with patch("ai_pipeline.macos_bridge.create_reminder", return_value={"success": True}), \
             patch("ai_pipeline.macos_bridge.display_notification") as notify:
            assistant._handle_create_reminder("milk", "")
            notify.assert_not_called()
            assistant.config.config["action_notifications"] = True
            assistant._handle_create_reminder("milk", "")
            notify.assert_called_once()

    def test_calendar_permission_errors_propagate_instead_of_claiming_empty_schedule(self):
        with patch("native_store.get_calendar_events", side_effect=PermissionError("Calendar access denied")):
            with self.assertRaisesRegex(PermissionError, "access denied"):
                macos_bridge.get_calendar_events(datetime.now(), datetime.now())


class InboxCostTests(unittest.TestCase):
    def test_repeated_inbox_reuses_bodies_classifications_and_summary(self):
        assistant = pipeline()
        assistant.classify_email_cleanup = Mock(return_value=("keep", .99))
        assistant._call_structured_llm = Mock(return_value=EmailSummarySlots(spoken_response="One work request."))
        def messages():
            return [{"id": "42", "sender": "Sam", "subject": "Project", "snippet": ""}]
        with patch("ai_pipeline.macos_bridge.get_email_full_body", return_value="Please review the project") as read:
            first, _ = assistant.cleanup_inbox(messages())
            original = assistant.summarize_emails(first)
            second, _ = assistant.cleanup_inbox(messages())
            self.assertEqual(assistant.summarize_emails(second), original)
        read.assert_called_once()
        assistant.classify_email_cleanup.assert_called_once()
        assistant._call_structured_llm.assert_called_once()

    def test_unavailable_cleanup_endpoint_has_retry_cooldown(self):
        assistant = pipeline()
        assistant.classify_email_cleanup = Mock(return_value=("keep", 0))
        messages = [{"id": "42", "content": "Important"}]
        assistant.cleanup_inbox(messages)
        assistant.cleanup_inbox(messages)
        assistant.classify_email_cleanup.assert_called_once()

    def test_full_inbox_evidence_budget_retains_every_message(self):
        assistant = pipeline()
        assistant._call_structured_llm = Mock(return_value=EmailSummarySlots(spoken_response="Routine messages."))
        emails = [{"id": str(index), "subject": f"Subject {index}", "content": "x" * 4000} for index in range(50)]
        assistant.summarize_emails(emails)
        evidence = assistant._call_structured_llm.call_args.args[0][1]["content"]
        self.assertLess(len(evidence), 28000)
        for index in range(50):
            self.assertIn(f"Subject {index}\n", evidence)

    def test_summary_cache_expires(self):
        assistant = pipeline()
        assistant._call_structured_llm = Mock(return_value=EmailSummarySlots(spoken_response="A request."))
        emails = [{"id": "1", "content": "Hello"}]
        with patch("ai_pipeline.time.monotonic", return_value=100):
            assistant.summarize_emails(emails)
        with patch("ai_pipeline.time.monotonic", return_value=2000):
            assistant.summarize_emails(emails)
        self.assertEqual(assistant._call_structured_llm.call_count, 2)


class AppBehaviorTests(unittest.TestCase):
    def app(self):
        return SimpleNamespace(is_recording=False, is_processing=False, _voice_generation=0,
            tts=Mock(), recorder=Mock(), menu_status=SimpleNamespace(title="Ready"),
            menu_toggle_record=SimpleNamespace(title="Record"), pipeline=Mock(), state=Mock(),
            _briefing_locks={"morning": threading.Lock(), "evening": threading.Lock()},
            _briefing_retry_after={"morning": 0, "evening": 0})

    def test_worker_ui_changes_are_queued_and_cannot_override_recording(self):
        app = self.app()
        app._ui_queue = queue.Queue()
        update_ui(app, status="Checking inbox", background=True)
        self.assertEqual(app.menu_status.title, "Ready")
        app.is_recording = True
        apply_ui(app, app._ui_queue.get_nowait())
        self.assertEqual(app.menu_status.title, "Ready")

    def test_microphone_failure_resumes_speech_and_leaves_recording_off(self):
        app = self.app()
        app.recorder.start.side_effect = RuntimeError("No microphone")
        with patch("main.macos_bridge.display_notification"):
            MacAssistantApp.start_recording(app)
        self.assertFalse(app.is_recording)
        app.tts.resume.assert_called_once()
        self.assertEqual(app.menu_status.title, "Microphone unavailable")

    def test_recorder_start_failure_closes_stream(self):
        recorder = AudioRecorder()
        stream = Mock()
        stream.start.side_effect = RuntimeError("Denied")
        with patch("main.sd.InputStream", return_value=stream):
            with self.assertRaisesRegex(RuntimeError, "Microphone unavailable"):
                recorder.start()
        self.assertFalse(recorder.recording)
        self.assertIsNone(recorder.stream)
        stream.close.assert_called_once()

    def test_briefing_cannot_spawn_duplicate_workers_and_weather_stays_unknown(self):
        app = self.app()
        workers = []
        def thread(target, **kwargs):
            workers.append(target)
            return SimpleNamespace(start=lambda: None)
        with patch("main.threading.Thread", side_effect=thread):
            MacAssistantApp.run_morning_briefing(app)
            MacAssistantApp.run_morning_briefing(app)
        self.assertEqual(len(workers), 1)
        with patch("main.requests.get", side_effect=RuntimeError("Offline")), \
             patch("main.macos_bridge.get_calendar_events", return_value=[]), \
             patch("main.macos_bridge.get_reminders", return_value=[]), \
             patch("main.macos_bridge.get_unread_emails", return_value=[]), \
             patch("main.macos_bridge.display_notification"):
            workers[0]()
        self.assertIn("Unavailable", app.pipeline.generate_morning_briefing.call_args.kwargs["weather"])
        self.assertFalse(app._briefing_locks["morning"].locked())

    def test_preferences_reload_updates_shortcut_and_client(self):
        app = self.app()
        app.config = Mock()
        app.config.ptt_hotkey = "cmd+5"
        app.config.tts_enabled = True
        def reload():
            app.config.ptt_hotkey = "f6"
            return True
        app.config.reload.side_effect = reload
        app._native_hotkey = Mock()
        app._hotkey_listener = None
        app._init_hotkey_listener = Mock()
        app.menu_tts = SimpleNamespace(title="")
        MacAssistantApp._refresh_settings(app, None)
        app.pipeline.refresh_config.assert_called_once()
        app._init_hotkey_listener.assert_called_once()

    def test_disabling_tts_stops_current_audio(self):
        app = self.app()
        app.config = Mock(tts_enabled=True)
        app.menu_tts = SimpleNamespace(title="")
        with patch("main.macos_bridge.display_notification"):
            MacAssistantApp.on_toggle_tts(app, None)
        app.tts.stop.assert_called_once()

    def test_native_say_respects_speed_and_treats_text_as_text(self):
        engine = engine_without_hardware()
        engine.kokoro = None
        with patch("tts_engine.subprocess.Popen") as process:
            engine._play_sync("-hello", "af_heart", 1.2, 0)
        self.assertEqual(process.call_args.args[0][-4:], ["-r", "210", "--", "-hello"])

    def test_config_reload_and_invalid_schedule_defaults(self):
        cfg = config.AppConfig.__new__(config.AppConfig)
        cfg.config = dict(config.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"poll_interval_minutes": 0, "morning_briefing_time": "29:99"}))
            with patch.object(config, "CONFIG_FILE", path):
                self.assertTrue(cfg.reload())
        self.assertEqual(cfg.poll_interval_minutes, 5)
        self.assertEqual(cfg.morning_briefing_time, "08:00")

    def test_assets_load_as_native_images(self):
        from AppKit import NSImage
        for name in ("idle.png", "recording.png", "busy.png", "AppIcon.icns"):
            with self.subTest(name=name):
                image = NSImage.alloc().initByReferencingFile_(str(ASSETS_DIR / name))
                self.assertTrue(image.isValid())


if __name__ == "__main__":
    unittest.main()
