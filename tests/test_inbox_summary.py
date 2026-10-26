"""Inbox summary regressions; no native app or audio operations are performed."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_pipeline import AIPipeline, EmailSummarySlots
from main import MacAssistantApp


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


class InboxSummaryTests(unittest.TestCase):
    def setUp(self):
        self.emails = [
            {"id": str(i), "sender": "newsletter@example.com", "subject": f"Promotion {i}", "snippet": ""}
            for i in range(8)
        ]

    def test_all_eight_previously_processed_messages_are_spoken(self):
        app = SimpleNamespace(
            _email_check_lock=threading.Lock(), _last_unread_snapshot=None,
            menu_status=SimpleNamespace(title=""),
            state=Mock(), pipeline=Mock(), tts=Mock(),
        )
        app.pipeline.cleanup_inbox.return_value = (self.emails, 0)
        app.state.is_email_processed.return_value = True
        app.pipeline.summarize_emails.return_value = "You have 8 unread emails. All eight are summarized."
        with patch("main.threading.Thread", ImmediateThread), \
             patch("main.macos_bridge.get_unread_emails", return_value=self.emails), \
             patch("main.macos_bridge.display_notification"), \
             patch("main.macos_bridge.create_calendar_event") as create_event:
            MacAssistantApp.run_email_triage(app)
            app.pipeline.summarize_emails.assert_called_once_with(self.emails)
            app.tts.speak.assert_called_once_with(app.pipeline.summarize_emails.return_value, block=True, generation=app.tts.generation)
            app.state.is_email_processed.assert_not_called()
            create_event.assert_not_called()
            # Unchanged scheduled checks stay quiet; a manual check always speaks.
            MacAssistantApp.run_email_triage(app)
            self.assertEqual(app.tts.speak.call_count, 1)
            MacAssistantApp.run_email_triage(app, manual=True)
            self.assertEqual(app.tts.speak.call_count, 2)
            self.assertFalse(app._email_check_lock.locked())

    def test_summary_includes_body_of_every_email_including_promotions(self):
        pipeline = AIPipeline.__new__(AIPipeline)
        pipeline._call_structured_llm = Mock(return_value=EmailSummarySlots(spoken_response="All eight promotions summarized."))
        with patch("ai_pipeline.macos_bridge.get_email_full_body", side_effect=lambda ident: f"Full body {ident}") as read:
            result = pipeline.summarize_emails(self.emails)
        self.assertEqual(read.call_count, 8)
        self.assertTrue(result.startswith("You have 8 unread emails:"))
        messages, schema = pipeline._call_structured_llm.call_args.args
        self.assertIs(schema, EmailSummarySlots)
        for i in range(8):
            self.assertIn(f"Promotion {i}", messages[1]["content"])
            self.assertIn(f"Full body {i}", messages[1]["content"])

    def test_voice_summary_fetches_current_unread_inbox(self):
        pipeline = AIPipeline.__new__(AIPipeline)
        pipeline.last_discussed_emails = [{"id": "stale"}]
        pipeline.summarize_emails = Mock(return_value="All eight emails.")
        pipeline.cleanup_inbox = Mock(return_value=(self.emails, 0))
        with patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=self.emails):
            result = pipeline._handle_summarize_inbox("summarize my inbox", "")
        self.assertEqual(result["unread_count"], 8)
        pipeline.summarize_emails.assert_called_once_with(self.emails)

    def test_ai_failure_keeps_summary_within_sentence_budget(self):
        pipeline = AIPipeline.__new__(AIPipeline)
        pipeline._call_structured_llm = Mock(side_effect=RuntimeError("offline"))
        with patch("ai_pipeline.macos_bridge.get_email_full_body", return_value=""):
            result = pipeline.summarize_emails(self.emails)
        self.assertIn("8 unread emails", result)
        self.assertEqual(result.count("."), 1)

    def test_overlong_model_reply_is_capped_at_two_sentences(self):
        pipeline = AIPipeline.__new__(AIPipeline)
        pipeline._call_structured_llm = Mock(return_value=EmailSummarySlots(spoken_response="First topic. Second topic. Third topic. Fourth topic."))
        with patch("ai_pipeline.macos_bridge.get_email_full_body", return_value=""):
            result = pipeline.summarize_emails(self.emails)
        self.assertEqual(result.count("."), 2)
        self.assertNotIn("Third", result)


if __name__ == "__main__":
    unittest.main()
