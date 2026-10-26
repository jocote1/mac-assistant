"""Background access regressions. No real accounts, Keychain items, or EventKit store."""

import imaplib
import unittest
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import Mock, patch

import EventKit
import Security

import imap_mail
import macos_bridge
import mail_credentials
import native_store
from ai_pipeline import DraftReplySlots
from test_improvements import pipeline


def message(uid="42", html=False):
    mail = EmailMessage()
    mail["From"] = "Säm <sam@example.com>"
    mail["To"] = "me@icloud.com"
    mail["Subject"] = "Plänning"
    mail["Message-ID"] = f"<message-{uid}@example.com>"
    mail["Date"] = "Mon, 26 Oct 2026 09:00:00 +0100"
    mail.set_content("Please review the meeting notes.")
    if html:
        mail = EmailMessage()
        mail.set_content("<style>hidden</style><p>Hello<br>world</p><script>bad()</script>", subtype="html")
    return mail.as_bytes()


class ICloudTests(unittest.TestCase):
    provider = "icloud"
    account = "me@icloud.com"
    host = "imap.mail.me.com"
    trash = '"Deleted Messages"'
    drafts = '"Drafts"'

    def setUp(self):
        self.connection = Mock()
        self.connection.login.return_value = ("OK", [])
        self.connection.select.return_value = ("OK", [b"2"])
        self.connection.response.return_value = ("UIDVALIDITY", [b"123"])
        self.connection.capabilities = (b"IMAP4rev1", b"MOVE", b"UIDPLUS")
        self.connection.capability.side_effect = lambda: ("OK", [b" ".join(self.connection.capabilities)])
        self.connection.list.return_value = ("OK", [
            f'(\\Trash \\HasNoChildren) "/" {self.trash}'.encode(),
            f'(\\Drafts \\HasNoChildren) "/" {self.drafts}'.encode(),
        ])
        self.connection.append.return_value = ("OK", [b"[APPENDUID 456 12]"])
        self.connection.uid.side_effect = self.uid
        self.body = message()
        self.identifier = imap_mail._Mailbox(self.connection, self.account, self.provider)
        self.identifier.validity = "123"
        self.identifier = self.identifier.identifier("42")
        for target, replacement in (
            ("config.AppConfig", Mock(return_value=Mock(get={"mail_provider": self.provider, "mail_email": self.account}.get))),
            ("imap_mail.get_password", Mock(return_value="app-password")),
            ("imap_mail.imaplib.IMAP4_SSL", Mock(return_value=self.connection)),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.scripts = patch("macos_bridge._run_applescript")
        self.script = self.scripts.start()
        self.addCleanup(self.scripts.stop)

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [b"41 42"]
        if command == "FETCH":
            return "OK", [(f"1 (UID {args[0]} BODY[] {{{len(self.body)}}}".encode(), self.body), b")"]
        return "OK", []

    def tearDown(self):
        self.script.assert_not_called()

    def test_headers_use_peek_tls_and_newest_first_without_marking_read(self):
        emails = macos_bridge.get_unread_emails(limit=1)
        self.assertEqual(emails[0]["id"], self.identifier)
        self.assertEqual(emails[0]["subject"], "Plänning")
        self.assertIn("Säm", emails[0]["sender"])
        self.connection.select.assert_called_once_with("INBOX", readonly=True)
        fetch = [c for c in self.connection.uid.call_args_list if c.args[0] == "FETCH"]
        self.assertEqual(len(fetch), 1)
        self.assertIn("BODY.PEEK[HEADER.FIELDS", fetch[0].args[2])
        self.assertEqual(fetch[0].args[1], "42")
        self.connection.logout.assert_called_once()
        imap_mail.imaplib.IMAP4_SSL.assert_called_once()
        self.assertEqual(imap_mail.imaplib.IMAP4_SSL.call_args.args, (self.host, 993))
        imap_mail.get_password.assert_called_once_with(self.account, self.provider)

    def test_body_reads_do_not_change_unread_status_or_include_attachments(self):
        mail = BytesParser(policy=policy.default).parsebytes(self.body)
        mail.add_attachment(b"secret attachment", maintype="text", subtype="plain", filename="notes.txt")
        self.body = mail.as_bytes()
        self.assertEqual(macos_bridge.get_email_full_body(self.identifier), "Please review the meeting notes.")
        self.assertIn("BODY.PEEK[]", self.connection.uid.call_args.args[2])

    def test_html_only_body_is_plain_text_without_scripts(self):
        self.body = message(html=True)
        text = macos_bridge.get_email_full_body(self.identifier)
        self.assertIn("Hello\nworld", text)
        self.assertNotIn("bad()", text)
        self.assertNotIn("hidden", text)

    def test_move_uses_exact_uid_and_special_use_trash(self):
        self.assertTrue(macos_bridge.trash_email_by_id(self.identifier)["success"])
        self.connection.uid.assert_any_call("MOVE", "42", self.trash)
        self.connection.expunge.assert_not_called()

    def test_stale_mailbox_account_and_injected_ids_never_mutate(self):
        for identifier in (self.identifier.replace(":123:", ":124:"), self.identifier.replace(f"{self.provider}:", f"{self.provider}:ffff"),
                           "42", "42; delete everything", self.identifier + ",43"):
            with self.subTest(identifier=identifier):
                self.connection.uid.reset_mock()
                result = macos_bridge.trash_email_by_id(identifier)
                self.assertFalse(result["success"])
                self.connection.uid.assert_not_called()

    def test_uidplus_never_expunge_other_deleted_messages(self):
        self.connection.capabilities = (b"UIDPLUS",)
        self.assertTrue(macos_bridge.trash_email_by_id(self.identifier)["success"])
        self.connection.uid.assert_any_call("COPY", "42", self.trash)
        self.connection.uid.assert_any_call("STORE", "42", "+FLAGS.SILENT", "(\\Deleted)")
        self.connection.uid.assert_any_call("EXPUNGE", "42")
        self.connection.expunge.assert_not_called()

    def test_no_safe_move_or_ambiguous_trash_never_deletes(self):
        self.connection.capabilities = ()
        result = macos_bridge.trash_email_by_id(self.identifier)
        self.assertFalse(result["success"])
        self.assertFalse(any(c.args[0] in ("MOVE", "STORE", "COPY") for c in self.connection.uid.call_args_list))

    def test_ambiguous_special_use_trash_is_rejected(self):
        self.connection.list.return_value = ("OK", [b'(\\Trash) "/" "Trash A"', b'(\\Trash) "/" "Trash B"'])
        result = macos_bridge.trash_email_by_id(self.identifier)
        self.assertFalse(result["success"])
        self.assertFalse(any(c.args[0] in ("MOVE", "STORE", "COPY") for c in self.connection.uid.call_args_list))

    def test_missing_or_changed_message_never_moves_anything(self):
        self.connection.uid.return_value = ("OK", [None])
        self.connection.uid.side_effect = None
        result = macos_bridge.trash_email_by_id(self.identifier)
        self.assertFalse(result["success"])
        self.assertEqual(self.connection.uid.call_count, 1)

    def test_auth_failure_is_error_instead_of_empty_inbox(self):
        self.connection.login.side_effect = imaplib.IMAP4.error("invalid password")
        with self.assertRaisesRegex(imap_mail.MailError, "sign-in failed"):
            macos_bridge.get_unread_emails()
        self.connection.select.assert_not_called()

    def test_missing_configuration_never_connects_or_launches_mail(self):
        with patch("config.AppConfig", return_value=Mock(get=Mock(return_value=""))):
            with self.assertRaisesRegex(imap_mail.MailError, "Preferences"):
                macos_bridge.get_unread_emails()
        imap_mail.imaplib.IMAP4_SSL.assert_not_called()

    def test_failed_search_is_error_instead_of_empty_inbox(self):
        self.connection.uid.side_effect = None
        self.connection.uid.return_value = ("NO", [b"error"])
        with self.assertRaisesRegex(imap_mail.MailError, "search"):
            macos_bridge.get_unread_emails()

    def test_reply_draft_has_thread_headers_and_never_sends(self):
        result = macos_bridge.create_draft_reply("Sam", "Plänning", "Looks good.", self.identifier)
        self.assertTrue(result["success"])
        folder, flags, _, raw = self.connection.append.call_args.args
        self.assertEqual((folder, flags), (self.drafts, "(\\Draft)"))
        draft = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual(draft["To"], "sam@example.com")
        self.assertEqual(draft["From"], self.account)
        self.assertEqual(draft["In-Reply-To"], "<message-42@example.com>")
        self.assertIn("<message-42@example.com>", draft["References"])
        self.assertEqual(draft["Subject"], "Re: Plänning")

    def test_unresolved_recipient_does_not_write_draft(self):
        result = macos_bridge.create_draft_reply("Sam", "Meeting", "Hi")
        self.assertFalse(result["success"])
        self.connection.append.assert_not_called()

    def test_append_failure_does_not_announce_success(self):
        self.connection.append.return_value = ("NO", [])
        result = macos_bridge.create_draft_reply("sam@example.com", "Meeting", "Hi")
        self.assertFalse(result["success"])


def event(identifier, start, end):
    item = Mock()
    item.eventIdentifier.return_value = identifier
    item.title.return_value = "Planning"
    item.startDate.return_value = native_store._date(start)
    item.endDate.return_value = native_store._date(end)
    item.calendar.return_value.title.return_value = "Work"
    item.location.return_value = ""
    item.notes.return_value = ""
    return item


class EventKitTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        patcher = patch("native_store._event_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("macos_bridge._run_applescript")
        self.script = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.script.assert_not_called()

    def test_overlap_query_includes_event_starting_before_window_and_sorts(self):
        ongoing = event("ongoing", self.now - timedelta(hours=2), self.now + timedelta(hours=1))
        later = event("later", self.now + timedelta(hours=2), self.now + timedelta(hours=3))
        self.store.eventsMatchingPredicate_.return_value = [later, ongoing]
        rows = macos_bridge.get_calendar_events(self.now, self.now + timedelta(days=1))
        self.assertEqual([r["id"] for r in rows], ["ongoing", "later"])
        args = self.store.predicateForEventsWithStartDate_endDate_calendars_.call_args.args
        self.assertEqual(args[0].timeIntervalSince1970(), self.now.timestamp())
        self.assertEqual(args[1].timeIntervalSince1970(), (self.now + timedelta(days=1)).timestamp())

    def test_long_windows_are_split_without_losing_or_duplicating_events(self):
        repeated = event("one", self.now, self.now + timedelta(hours=1))
        self.store.eventsMatchingPredicate_.return_value = [repeated]
        rows = macos_bridge.get_calendar_events(self.now, self.now + timedelta(days=365 * 8))
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.store.eventsMatchingPredicate_.call_count, 3)

    def test_missing_named_calendar_is_error(self):
        self.store.calendarsForEntityType_.return_value = []
        with self.assertRaisesRegex(ValueError, "not found"):
            macos_bridge.get_calendar_events(self.now, self.now + timedelta(days=1), "Missing")

    def test_event_write_reports_native_failure_without_fallback(self):
        self.store.saveEvent_span_commit_error_.return_value = (False, Mock(localizedDescription=Mock(return_value="Calendar is read-only")))
        with patch("native_store.EventKit.EKEvent"):
            result = macos_bridge.create_calendar_event("Work", self.now.isoformat(), (self.now + timedelta(hours=1)).isoformat())
        self.assertFalse(result["success"])
        self.assertIn("read-only", result["error"])

    def test_invalid_event_range_never_writes(self):
        result = macos_bridge.create_calendar_event("Bad", self.now.isoformat(), (self.now - timedelta(hours=1)).isoformat())
        self.assertFalse(result["success"])
        self.store.saveEvent_span_commit_error_.assert_not_called()

    def test_ambiguous_deletion_never_mutates(self):
        self.store.eventsMatchingPredicate_.return_value = [event("1", self.now, self.now), event("2", self.now, self.now)]
        result = macos_bridge.delete_calendar_event("Planning", self.now.isoformat(), (self.now + timedelta(days=1)).isoformat())
        self.assertFalse(result["success"])
        self.store.removeEvent_span_commit_error_.assert_not_called()

    def test_delete_one_occurrence_uses_requested_date_and_commits(self):
        item = event("one", self.now, self.now + timedelta(hours=1))
        self.store.eventsMatchingPredicate_.return_value = [item]
        self.store.removeEvent_span_commit_error_.return_value = (True, None)
        result = macos_bridge.delete_calendar_event("Planning", self.now.isoformat(), (self.now + timedelta(days=1)).isoformat())
        self.assertTrue(result["success"])
        self.store.removeEvent_span_commit_error_.assert_called_once_with(item, EventKit.EKSpanThisEvent, True, None)

    def test_reminders_include_undated_but_filter_completed_and_later(self):
        open_reminder = Mock()
        open_reminder.isCompleted.return_value = False
        open_reminder.dueDateComponents.return_value = None
        open_reminder.calendarItemIdentifier.return_value = "open"
        completed = Mock()
        completed.isCompleted.return_value = True
        self.store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda predicate, complete: complete([open_reminder, completed])
        rows = macos_bridge.get_reminders(due_before=self.now)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["due_date"])

    def test_ambiguous_completion_never_changes_any_reminder(self):
        reminders = [Mock(), Mock()]
        for reminder in reminders:
            reminder.isCompleted.return_value = False
            reminder.title.return_value = "Milk"
        self.store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda predicate, complete: complete(reminders)
        self.assertFalse(macos_bridge.complete_reminder("Milk")["success"])
        for reminder in reminders:
            reminder.setCompleted_.assert_not_called()

    def test_access_denied_is_visible_instead_of_no_events(self):
        with patch("native_store._event_store", side_effect=PermissionError("Calendar access denied")):
            with self.assertRaises(PermissionError):
                macos_bridge.get_calendar_events(self.now, self.now + timedelta(days=1))

    def test_reminder_due_time_survives_native_components_roundtrip(self):
        reminder = Mock()
        reminder.isCompleted.return_value = False
        reminder.dueDateComponents.side_effect = lambda: reminder.setDueDateComponents_.call_args.args[0]
        self.store.saveReminder_commit_error_.return_value = (True, None)
        with patch("native_store.EventKit.EKReminder") as factory:
            factory.reminderWithEventStore_.return_value = reminder
            result = macos_bridge.create_reminder("Meeting", "2026-10-26T15:30:00+01:00", priority=5)
        self.assertTrue(result["success"])
        self.assertEqual(datetime.fromisoformat(result["due_date"]).timestamp(), datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc).timestamp())
        self.store.saveReminder_commit_error_.assert_called_once_with(reminder, True, None)

    def test_completes_only_unique_incomplete_reminder(self):
        reminder = Mock()
        reminder.isCompleted.return_value = False
        reminder.title.return_value = "Milk"
        reminder.dueDateComponents.return_value = None
        self.store.fetchRemindersMatchingPredicate_completion_.side_effect = lambda predicate, complete: complete([reminder])
        self.store.saveReminder_commit_error_.return_value = (True, None)
        self.assertTrue(macos_bridge.complete_reminder("Milk")["success"])
        reminder.setCompleted_.assert_called_once_with(True)
        self.store.saveReminder_commit_error_.assert_called_once_with(reminder, True, None)


class PermissionAndKeychainTests(unittest.TestCase):
    def test_denied_permission_never_prompts_again(self):
        store = Mock()
        with patch("native_store._store", store), patch("native_store.EventKit.EKEventStore") as event_store:
            event_store.authorizationStatusForEntityType_.return_value = EventKit.EKAuthorizationStatusDenied
            with self.assertRaisesRegex(PermissionError, "System Settings"):
                native_store._event_store(EventKit.EKEntityTypeEvent)
        store.requestFullAccessToEventsWithCompletion_.assert_not_called()

    def test_first_access_requests_full_permission(self):
        store = Mock()
        store.requestFullAccessToEventsWithCompletion_.side_effect = lambda complete: complete(True, None)
        with patch("native_store._store", store), patch("native_store.EventKit.EKEventStore") as event_store:
            event_store.authorizationStatusForEntityType_.return_value = EventKit.EKAuthorizationStatusNotDetermined
            self.assertIs(native_store._event_store(EventKit.EKEntityTypeEvent), store)
        store.requestFullAccessToEventsWithCompletion_.assert_called_once()

    def test_keychain_stores_password_as_data_and_reads_without_cli(self):
        with patch("mail_credentials.Security.SecItemUpdate", return_value=Security.errSecItemNotFound), \
             patch("mail_credentials.Security.SecItemAdd", return_value=(0, None)) as add, \
             patch("mail_credentials.Security.SecItemCopyMatching", return_value=(0, b"private-password")):
            mail_credentials.save_password("ME@icloud.com", "private-password")
            self.assertEqual(mail_credentials.get_password("me@icloud.com"), "private-password")
        query = add.call_args.args[0]
        self.assertEqual(query[Security.kSecAttrAccount], "me@icloud.com")
        self.assertEqual(query[Security.kSecValueData], b"private-password")

    def test_missing_or_locked_keychain_is_distinct_from_empty_password(self):
        with patch("mail_credentials.Security.SecItemCopyMatching", return_value=(Security.errSecItemNotFound, None)):
            self.assertIsNone(mail_credentials.get_password("me@icloud.com"))
        with patch("mail_credentials.Security.SecItemCopyMatching", return_value=(Security.errSecAuthFailed, None)):
            with self.assertRaisesRegex(RuntimeError, "Keychain"):
                mail_credentials.get_password("me@icloud.com")

    def test_reply_reference_passes_exact_original_id(self):
        assistant = pipeline()
        assistant._remember_email_list([{"id": "icloud:abc:123:42", "sender": "Sam", "subject": "Meeting"}])
        assistant._call_structured_llm = Mock(return_value=DraftReplySlots(
            recipient_email_or_name="Sam", subject="Meeting", reply_body="Hi", spoken_response="",
            source_message_reference="first email"))
        with patch("ai_pipeline.macos_bridge.create_draft_reply", return_value={"success": True}) as draft:
            self.assertTrue(assistant._handle_draft_reply("Reply to the first email", "")["executed"])
        self.assertEqual(draft.call_args.kwargs["original_message_id"], "icloud:abc:123:42")
