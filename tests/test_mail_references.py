"""Reproduce spoken email references without reading, opening or deleting real mail."""
import time
import unittest
from unittest.mock import Mock, patch
import macos_bridge

from ai_pipeline import (
    ActionPlan, PlannedAction, DeleteItemSlots, EmailSummarySlots, OpenMailSlots, ReadMailSlots,
)
from test_improvements import pipeline


def email(identifier, sender, subject):
    return {"id": str(identifier), "sender": sender, "subject": subject, "content": "Message body"}


class MailReferenceTests(unittest.TestCase):
    def setUp(self):
        self.assistant = pipeline()
        self.pixelforge = email(10, "PixelForge <hello@pixelforge.example.com>", "Your monthly design update")
        self.cloudservice = email(11, "CloudService <no-reply@cloudservice.example.com>", "Application setup reminder")
        self.career_network = email(12, "CareerNetwork", "Your weekly career newsletter")
        self.emails = [self.pixelforge, self.cloudservice, self.career_network]
        self.assistant._remember_email_list(self.emails)
        self.assistant._call_structured_llm = Mock(return_value=ReadMailSlots(spoken_response=""))

    def test_combined_sender_and_subject_deletes_exact_message(self):
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash, \
             patch("ai_pipeline.macos_bridge.delete_emails") as keyword_delete:
            result = self.assistant._handle_delete_mail("CloudService: Application setup reminder", "")
        trash.assert_called_once_with("11")
        keyword_delete.assert_not_called()
        self.assistant._call_structured_llm.assert_not_called()
        self.assertTrue(result["executed"])

    def test_pixelforge_cloudservice_delete_that_one_sequence(self):
        first = self.assistant._handle_read_mail("read the first email", "")
        self.assertEqual(first["emails"][0]["id"], "10")
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            self.assistant._handle_delete_mail("delete that email from PixelForge", "")
            with patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=[self.cloudservice, self.career_network]):
                second = self.assistant._handle_read_mail("any others?", "")
            self.assertEqual(second["emails"][0]["id"], "11")
            result = self.assistant._handle_delete_mail("you can delete that one as well", "")
        self.assertEqual([call.args[0] for call in trash.call_args_list], ["10", "11"])
        self.assertTrue(result["executed"])
        self.assertNotIn(self.cloudservice, self.assistant.last_discussed_emails)
        self.assertIsNone(self.assistant.last_read_email)

    def test_next_email_skips_previously_read_email_even_when_not_deleted(self):
        self.assistant._handle_read_mail("read first email", "")
        with patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=self.emails):
            result = self.assistant._handle_read_mail("read another email", "")
        self.assertEqual(result["emails"][0]["id"], "11")

    def test_original_that_one_reference_overrides_stale_planner_rewrite(self):
        self.assistant._handle_read_mail("read second email", "")
        self.assistant.classify_action_plan = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="delete_mail", request="delete the PixelForge email"),
            PlannedAction(intent="general_query", request="any others", reply="There are more emails."),
        ]))
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            self.assistant.execute_pipeline("You can delete that one as well. Any others?")
        trash.assert_called_once_with("11")

    def test_delete_and_any_others_cannot_reuse_previous_sender_filter(self):
        self.assistant._handle_read_mail("read first email", "")
        self.assistant.classify_action_plan = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="delete_mail", request="delete PixelForge"),
            PlannedAction(intent="read_mail", request="read another PixelForge email"),
        ]))
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}), \
             patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=[self.cloudservice, self.career_network]):
            result = self.assistant.execute_pipeline("Delete that email. Any others?")
        self.assertEqual(result["actions"][1]["emails"][0]["id"], "11")
        self.assertIn("CloudService", result["spoken_response"])

    def test_delete_all_those_uses_latest_summary_and_not_old_deleted_topics(self):
        self.assistant.conversation_history = [
            {"role": "user", "content": "delete StreamService and VideoService"},
            {"role": "assistant", "content": "Moved those emails to Trash."},
        ]
        self.assistant._remember_email_list([self.cloudservice, self.career_network])
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            result = self.assistant._handle_delete_mail("delete all those emails", "")
        self.assertEqual([call.args[0] for call in trash.call_args_list], ["11", "12"])
        self.assertEqual(result["os_result"]["count"], 2)
        self.assertEqual(self.assistant.last_discussed_emails, [])

    def test_plural_reference_after_one_read_cannot_delete_unspoken_messages(self):
        self.assistant._handle_read_mail("read first email", "")
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            self.assistant._handle_delete_mail("delete those emails", "")
        trash.assert_called_once_with("10")

    def test_background_summary_context_survives_first_voice_request(self):
        self.assistant._call_structured_llm.return_value = EmailSummarySlots(spoken_response="An application reminder and updates.")
        self.assistant.summarize_emails(self.emails)
        self.assistant.last_turn_timestamp = 0
        self.assistant.classify_action_plan = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="delete_mail", request="CloudService: Application setup reminder")]))
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            self.assistant.execute_pipeline("delete the CloudService email")
        trash.assert_called_once_with("11")

    def test_ambiguous_sender_asks_instead_of_deleting_multiple_messages(self):
        self.assistant._remember_email_list([self.cloudservice, email(13, "CloudService", "Weekly update")])
        with patch("ai_pipeline.macos_bridge.trash_email_by_id") as trash:
            result = self.assistant._handle_delete_mail("delete the email from CloudService", "")
        trash.assert_not_called()
        self.assertFalse(result["executed"])
        self.assertIn("2 matching emails", result["spoken_response"])

    def test_reference_without_focused_message_asks_which_email(self):
        with patch("ai_pipeline.macos_bridge.trash_email_by_id") as trash:
            result = self.assistant._handle_delete_mail("delete that email", "")
        trash.assert_not_called()
        self.assertFalse(result["executed"])

    def test_failed_deletion_preserves_reference_for_retry(self):
        self.assistant._handle_read_mail("read the second email", "")
        with patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": False}):
            result = self.assistant._handle_delete_mail("delete that email", "")
        self.assertFalse(result["executed"])
        self.assertEqual(self.assistant.last_read_email["id"], "11")

    def test_named_combined_query_can_resolve_fresh_inbox_without_memory(self):
        self.assistant._remember_email_list([])
        with patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=[self.cloudservice]), \
             patch("ai_pipeline.macos_bridge.trash_email_by_id", return_value={"success": True}) as trash:
            result = self.assistant._handle_delete_mail("delete CloudService Application setup reminder", "")
        trash.assert_called_once_with("11")
        self.assertTrue(result["executed"])

    def test_open_that_email_uses_last_read_id_and_not_keyword_search(self):
        self.assistant._handle_read_mail("read second email", "")
        self.assistant._call_structured_llm.return_value = OpenMailSlots(message_query="that email", spoken_response="")
        with patch("ai_pipeline.macos_bridge.open_mail_app", return_value=True) as open_mail:
            result = self.assistant._handle_open_mail("open it", "")
        open_mail.assert_called_once_with(message_id="11", search_query=None)
        self.assertTrue(result["executed"])

    def test_unknown_email_never_claims_to_have_opened_it(self):
        self.assistant._call_structured_llm.return_value = OpenMailSlots(message_query="Missing Sender", spoken_response="Opened it.")
        with patch("ai_pipeline.macos_bridge.get_unread_emails", return_value=self.emails), \
             patch("ai_pipeline.macos_bridge.open_mail_app") as open_mail:
            result = self.assistant._handle_open_mail("open Missing Sender", "")
        open_mail.assert_not_called()
        self.assertFalse(result["executed"])

    def test_message_moved_since_summary_cannot_report_open_success(self):
        for query in ({"message_id": "11"}, {"search_query": "Application setup reminder"}):
            with self.subTest(query=query), patch("macos_bridge._run_applescript", return_value=(0, "not_found", "")):
                self.assertFalse(macos_bridge.open_mail_app(**query))
            with self.subTest(query=query), patch("macos_bridge._run_applescript", return_value=(0, "opened", "")):
                self.assertTrue(macos_bridge.open_mail_app(**query))

    def test_unresolved_read_reference_cannot_read_an_arbitrary_inbox_message(self):
        with patch("ai_pipeline.macos_bridge.get_unread_emails") as read:
            result = self.assistant._handle_read_mail("read that email", "")
        read.assert_not_called()
        self.assertFalse(result["executed"])

    def test_expired_context_does_not_delete_old_references(self):
        self.assistant.last_mail_timestamp = time.time() - 600
        self.assistant.classify_action_plan = Mock(return_value=ActionPlan(actions=[
            PlannedAction(intent="delete_mail", request="delete that email")]))
        with patch("ai_pipeline.macos_bridge.trash_email_by_id") as trash:
            result = self.assistant.execute_pipeline("delete that email")
        trash.assert_not_called()
        self.assertFalse(result["executed"])

    def test_named_ordinal_cannot_select_an_unrelated_sender(self):
        self.assistant._handle_read_mail("read first email from CloudService", "")
        self.assertEqual(self.assistant.last_read_email["id"], "11")


if __name__ == "__main__":
    unittest.main()
