"""Gmail regressions using fake IMAP and Keychain services only."""

import imaplib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import Security

import config
import imap_mail
import macos_bridge
import mail_credentials
import test_background_access as background
from test_improvements import pipeline


class GmailTests(background.ICloudTests):
    provider = "gmail"
    account = "me@gmail.com"
    host = "imap.gmail.com"
    trash = '"[Gmail]/Trash"'
    drafts = '"[Gmail]/Drafts"'

    def test_localized_special_use_folders(self):
        self.connection.list.return_value = ("OK", [
            b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
            b'(\\Trash \\HasNoChildren) "/" "[Gmail]/Papierkorb"',
            b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Entw&APw-rfe"',
        ])
        self.assertTrue(macos_bridge.trash_email_by_id(self.identifier)["success"])
        self.connection.uid.assert_any_call("MOVE", "42", '"[Gmail]/Papierkorb"')
        self.assertTrue(macos_bridge.create_draft_reply("sam@example.com", "Meeting", "Hi")["success"])
        self.assertEqual(self.connection.append.call_args.args[0], '"[Gmail]/Entw&APw-rfe"')

    def test_login_refreshes_capabilities_before_moving(self):
        self.connection.capabilities = (b"IMAP4rev1",)
        self.connection.capability.side_effect = None
        self.connection.capability.return_value = ("OK", [b"IMAP4rev1 MOVE UIDPLUS"])
        self.assertTrue(macos_bridge.trash_email_by_id(self.identifier)["success"])
        self.connection.capability.assert_called_once()
        self.connection.uid.assert_any_call("MOVE", "42", self.trash)

    def test_other_provider_reference_is_rejected_before_fetch_or_write(self):
        result = macos_bridge.trash_email_by_id(self.identifier.replace("gmail:", "icloud:"))
        self.assertFalse(result["success"])
        self.connection.uid.assert_not_called()

    def test_connection_test_normalizes_grouped_google_app_password(self):
        imap_mail.test_connection("ME@gmail.com", "abcd efgh ijkl mnop", "gmail")
        self.connection.login.assert_called_once_with("me@gmail.com", "abcdefghijklmnop")
        self.connection.select.assert_called_once_with("INBOX", readonly=True)
        self.connection.uid.assert_not_called()
        self.connection.append.assert_not_called()
        imap_mail.get_password.assert_not_called()

    def test_sign_in_failure_does_not_expose_server_echoed_password(self):
        self.connection.login.side_effect = imaplib.IMAP4.error("bad password private-secret")
        with self.assertRaises(imap_mail.MailError) as failure:
            imap_mail.test_connection(self.account, "private-secret", "gmail")
        self.assertNotIn("private-secret", str(failure.exception))
        self.assertIn("Gmail sign-in failed", str(failure.exception))
        self.connection.logout.assert_called_once()

    def test_bad_provider_never_uses_saved_credentials_or_connects(self):
        with patch("config.AppConfig", return_value=Mock(get={"mail_provider": "unknown", "mail_email": self.account}.get)):
            with self.assertRaisesRegex(ValueError, "Choose Gmail or iCloud"):
                macos_bridge.get_unread_emails()
        imap_mail.get_password.assert_not_called()
        imap_mail.imaplib.IMAP4_SSL.assert_not_called()


class GmailPreferencesTests(unittest.TestCase):
    def test_existing_icloud_config_migrates_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"icloud_email": "me@icloud.com", "ptt_hotkey": "f6"}))
            with patch.object(config, "CONFIG_FILE", path):
                settings = config.AppConfig.__new__(config.AppConfig).load()
        self.assertEqual(settings["mail_provider"], "icloud")
        self.assertEqual(settings["mail_email"], "me@icloud.com")
        self.assertEqual(settings["ptt_hotkey"], "f6")

    def test_gmail_is_default_for_new_and_existing_unconfigured_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for saved in ({}, {"icloud_email": ""}):
                with self.subTest(saved=saved):
                    path.write_text(json.dumps(saved))
                    with patch.object(config, "CONFIG_FILE", path):
                        settings = config.AppConfig.__new__(config.AppConfig).load()
                    self.assertEqual(settings["mail_provider"], "gmail")
                    self.assertEqual(settings["mail_email"], "")

    def test_keychain_provider_separation_and_password_normalization(self):
        with patch("mail_credentials.Security.SecItemUpdate", return_value=Security.errSecItemNotFound), \
             patch("mail_credentials.Security.SecItemAdd", return_value=(0, None)) as add:
            mail_credentials.save_password("ME@gmail.com", "abcd efgh ijkl mnop", "gmail")
            gmail = add.call_args.args[0]
            mail_credentials.save_password("ME@gmail.com", "icloud-password", "icloud")
            icloud = add.call_args.args[0]
        self.assertNotEqual(gmail[Security.kSecAttrService], icloud[Security.kSecAttrService])
        self.assertEqual(gmail[Security.kSecAttrAccount], "me@gmail.com")
        self.assertEqual(gmail[Security.kSecValueData], b"abcdefghijklmnop")

    def test_switching_accounts_clears_previous_mail_context_and_caches(self):
        assistant = pipeline()
        assistant._remember_email_list([{"id": "old-account-message"}])
        assistant.last_read_email = {"id": "old-account-message"}
        assistant._body_cache["old-account-message"] = (0, "private message")
        assistant._summary_cache["old-account-summary"] = (0, "private summary")
        assistant.conversation_history = [{"role": "assistant", "content": "private summary"}]
        assistant.config.config.update(mail_provider="gmail", mail_email="new@gmail.com")
        assistant.refresh_config()
        self.assertEqual(assistant.last_discussed_emails, [])
        self.assertEqual(assistant.last_presented_emails, [])
        self.assertIsNone(assistant.last_read_email)
        self.assertEqual(assistant._body_cache, {})
        self.assertEqual(assistant._summary_cache, {})
        self.assertEqual(assistant.conversation_history, [])

    def test_explicit_open_resolves_gmail_id_to_apple_mail_message_id(self):
        with patch("imap_mail.get_message_id", return_value="<gmail-message@example.com>") as resolve, \
             patch("macos_bridge._run_applescript", return_value=(0, "opened", "")) as script:
            self.assertTrue(macos_bridge.open_mail_app("gmail:123abc:123:42"))
        resolve.assert_called_once_with("gmail:123abc:123:42")
        self.assertEqual(script.call_args.kwargs["args"], ["gmail-message@example.com"])
