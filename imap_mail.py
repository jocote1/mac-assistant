"""Gmail and iCloud IMAP operations that never launch Mail or send messages."""

import hashlib
import imaplib
import re
import ssl
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from mail_credentials import get_password, normalize_password


PROVIDERS = {
    "gmail": ("Gmail", "imap.gmail.com"),
    "icloud": ("iCloud", "imap.mail.me.com"),
}


def validate_account(account, provider):
    if provider not in PROVIDERS:
        raise ValueError("Choose Gmail or iCloud in Preferences → Mail.")
    account = account.strip().lower()
    if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", account):
        raise ValueError("Enter your full email address in Preferences → Mail.")
    return account


class MailError(RuntimeError):
    pass


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "br", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _checked(result, operation):
    status, data = result
    if status != "OK":
        raise MailError(f"Mail could not {operation}.")
    return data


class _Mailbox:
    def __init__(self, connection, account, provider="icloud"):
        self.connection = connection
        self.account = account
        self.provider = provider
        self.account_id = hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
        self.validity = None

    def select(self, readonly=True):
        _checked(self.connection.select("INBOX", readonly=readonly), "open the inbox")
        _, data = self.connection.response("UIDVALIDITY")
        if not data or not data[0] or not re.fullmatch(rb"[1-9]\d*", data[0]):
            raise MailError("Mail did not provide a valid mailbox identity.")
        self.validity = data[0].decode("ascii")

    def identifier(self, uid):
        return f"{self.provider}:{self.account_id}:{self.validity}:{uid}"

    def uid(self, identifier):
        match = re.fullmatch(rf"{re.escape(self.provider)}:([0-9a-f]{{16}}):([1-9]\d*):([1-9]\d*)", str(identifier))
        if not match or match[1] != self.account_id or match[2] != self.validity:
            raise MailError("Email reference is stale or belongs to another account. Refresh the inbox first.")
        return match[3]

    def search(self, criteria):
        data = _checked(self.connection.uid("SEARCH", None, criteria), "search the inbox")
        return data[0].decode("ascii").split() if data and data[0] else []

    def fetch(self, uid, headers=False):
        fields = "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID REFERENCES REPLY-TO)]" if headers else "BODY.PEEK[]"
        data = _checked(self.connection.uid("FETCH", uid, f"(UID {fields})"), "read the message")
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                match = re.search(rb"\bUID (\d+)\b", part[0])
                if match and match[1].decode("ascii") == uid:
                    return BytesParser(policy=policy.default).parsebytes(part[1])
        raise MailError("That message is no longer in the inbox. Refresh the inbox first.")

    def folder(self, special, names):
        data = _checked(self.connection.list(), "find mailbox folders")
        special_matches = []
        fallback = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            match = re.fullmatch(rb'\(([^)]*)\) (?:NIL|"(?:[^"\\]|\\.)*") (.+)', line)
            if not match:
                continue
            flags, mailbox = match.groups()
            if b"\\noselect" in flags.lower().split():
                continue
            raw = mailbox.decode("ascii")
            if special.lower().encode("ascii") in flags.lower().split():
                special_matches.append(raw)
            if raw.strip('"').casefold() in names:
                fallback.append(raw)
        if len(special_matches) == 1:
            return special_matches[0]
        if not special_matches and len(fallback) == 1:
            return fallback[0]
        raise MailError(f"The {special.lstrip(chr(92))} folder was not found uniquely.")

    def trash(self, uid):
        self.fetch(uid, headers=True)
        folder = self.folder("\\Trash", {"deleted messages", "trash"})
        capabilities = {c.upper() if isinstance(c, bytes) else c.upper().encode("ascii") for c in self.connection.capabilities}
        if b"MOVE" in capabilities:
            _checked(self.connection.uid("MOVE", uid, folder), "move the message to Trash")
        elif b"UIDPLUS" in capabilities:
            _checked(self.connection.uid("COPY", uid, folder), "copy the message to Trash")
            _checked(self.connection.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"), "mark the copied message for removal from the inbox")
            # UID EXPUNGE avoids deleting other messages already flagged by another mail client.
            _checked(self.connection.uid("EXPUNGE", uid), "finish removing the copied message from the inbox")
        else:
            raise MailError("The mail server does not advertise safe message moves (MOVE or UIDPLUS).")


@contextmanager
def _session():
    from config import AppConfig
    config = AppConfig()
    provider = config.get("mail_provider", "gmail")
    account = config.get("mail_email", "").strip().lower()
    if not account:
        raise MailError("Set your email address and app password in Preferences → Mail.")
    account = validate_account(account, provider)
    password = get_password(account, provider)
    if not password:
        raise MailError("Save an app password in Preferences → Mail.")
    with _connect(account, password, provider) as connection:
        yield _Mailbox(connection, account, provider)


@contextmanager
def _connect(account, password, provider):
    account = validate_account(account, provider)
    label, host = PROVIDERS[provider]
    password = normalize_password(password, provider)
    if not password:
        raise ValueError("Enter an app password in Preferences → Mail.")
    connection = None
    try:
        connection = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=20)
        try:
            _checked(connection.login(account, password), "sign in")
        except imaplib.IMAP4.error:
            raise MailError(f"{label} sign-in failed. Check your address and app password in Preferences → Mail.") from None
        capabilities = _checked(connection.capability(), "check server capabilities")
        connection.capabilities = tuple(b" ".join(capabilities).upper().split())
        yield connection
    except (imaplib.IMAP4.error, OSError) as error:
        raise MailError(f"{label} connection failed ({type(error).__name__}). Check your connection and retry.") from None
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                connection.shutdown()


def test_connection(account, password, provider="gmail"):
    with _connect(account, password, provider) as connection:
        _checked(connection.select("INBOX", readonly=True), "open the inbox")


def _header(mailbox, uid, message):
    date = None
    if message.get("Date"):
        try:
            date = parsedate_to_datetime(str(message["Date"])).astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            pass
    return {"id": mailbox.identifier(uid), "sender": str(message.get("From", "")),
            "subject": str(message.get("Subject", "")), "date_received": date.isoformat() if date else "",
            "message_id": str(message.get("Message-ID", "")), "snippet": ""}


def get_unread_emails(since_iso=None, limit=40):
    limit = max(0, min(int(limit), 100))
    if not limit:
        return []
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00")).astimezone(timezone.utc) if since_iso else None
    with _session() as mailbox:
        mailbox.select()
        uids = mailbox.search("UNSEEN")
        results = []
        for uid in reversed(uids):
            data = _header(mailbox, uid, mailbox.fetch(uid, headers=True))
            if since and data["date_received"] and datetime.fromisoformat(data["date_received"]) < since:
                continue
            results.append(data)
            if len(results) >= limit:
                break
        return results


def get_email_full_body(message_id):
    with _session() as mailbox:
        mailbox.select()
        message = mailbox.fetch(mailbox.uid(message_id))
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(content)
        content = "".join(parser.parts)
    return content.strip()


def get_message_id(message_id):
    with _session() as mailbox:
        mailbox.select()
        message = mailbox.fetch(mailbox.uid(message_id), headers=True)
        result = str(message.get("Message-ID", "")).strip()
        if not result:
            raise MailError("That message has no Message-ID for opening in Apple Mail.")
        return result


def trash_email_by_id(message_id):
    with _session() as mailbox:
        mailbox.select(readonly=False)
        mailbox.trash(mailbox.uid(message_id))
    return {"success": True, "id": message_id}


def delete_emails(query):
    query = query.strip().casefold()
    if not query:
        raise ValueError("No email sender or subject was provided.")
    count = 0
    with _session() as mailbox:
        mailbox.select(readonly=False)
        for uid in mailbox.search("ALL"):
            message = mailbox.fetch(uid, headers=True)
            if any(query in str(message.get(header, "")).casefold() for header in ("From", "Subject")):
                try:
                    mailbox.trash(uid)
                except MailError as error:
                    return {"success": False, "count": count, "error": str(error)}
                count += 1
    return {"success": True, "count": count}


def create_draft_reply(recipient, subject, reply_body, original_message_id=None):
    with _session() as mailbox:
        original = None
        if original_message_id:
            mailbox.select()
            original = mailbox.fetch(mailbox.uid(original_message_id), headers=True)
        _, address = parseaddr(recipient)
        if "@" not in address and original:
            _, address = parseaddr(str(original.get("Reply-To") or original.get("From", "")))
        if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address):
            raise ValueError("Please specify the recipient's email address, or which email to reply to.")
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = mailbox.account
        message["To"] = address
        message["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid()
        if original and original.get("Message-ID"):
            message["In-Reply-To"] = str(original["Message-ID"])
            message["References"] = (str(original.get("References", "")) + " " + str(original["Message-ID"])).strip()
        message.set_content(reply_body)
        folder = mailbox.folder("\\Drafts", {"drafts"})
        _checked(mailbox.connection.append(folder, "(\\Draft)", None, message.as_bytes()), "save the draft")
        return {"success": True, "id": str(message["Message-ID"]), "subject": str(message["Subject"]), "recipient": address}
