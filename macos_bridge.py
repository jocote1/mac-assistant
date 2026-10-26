"""
Native background Calendar, Reminders and Gmail/iCloud Mail integration.

AppleScript is used only for notifications and explicitly opening Mail.
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from objc import error as ObjCError
import imap_mail
import native_store


def _write(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (ValueError, RuntimeError, OSError, ObjCError) as error:
        return {"success": False, "error": str(error)}


def _run_applescript(script: str, timeout: int = 30, args: Optional[List[str]] = None) -> Tuple[int, str, str]:
    """Run AppleScript with subprocess."""
    try:
        proc = subprocess.run(["osascript", "-e", script, *(args or [])], capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


# =====================================================================
# 1. macOS Native Notifications
# =====================================================================

def display_notification(title: str, message: str, subtitle: str = "", sound: str = "default") -> bool:
    """
    Display a native macOS banner notification safely.
    """
    safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
    safe_msg = message.replace('\\', '\\\\').replace('"', '\\"')
    safe_sub = subtitle.replace('\\', '\\\\').replace('"', '\\"')

    sub_clause = f'subtitle "{safe_sub}"' if subtitle else ""
    sound_clause = f'sound name "{sound}"' if sound else ""

    script = f'display notification "{safe_msg}" with title "{safe_title}" {sub_clause} {sound_clause}'
    code, _, err = _run_applescript(script)
    if code != 0:
        print(f"[Bridge] Notification failed: {err}", file=sys.stderr)
        return False
    return True


# =====================================================================
# 2. Apple Calendar
# =====================================================================

def get_calendar_events(start_dt: datetime, end_dt: datetime, calendar_name: Optional[str] = None) -> List[Dict[str, Any]]:
    return native_store.get_calendar_events(start_dt, end_dt, calendar_name)


def create_calendar_event(title: str, start_iso: str, end_iso: str, location: str = "", notes: str = "", calendar_name: Optional[str] = None) -> Dict[str, Any]:
    return _write(native_store.create_calendar_event, title, start_iso, end_iso, location, notes, calendar_name)


def delete_calendar_event(title_query: str, start_iso: Optional[str] = None, end_iso: Optional[str] = None) -> Dict[str, Any]:
    return _write(native_store.delete_calendar_event, title_query, start_iso, end_iso)


def get_calendar_conflicts(
    target_start_iso: str,
    target_end_iso: str,
    window_hours: int = 24
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Retrieve all events in the ±window_hours window around the target start and end.
    Returns (conflicting_events, surrounding_events).
    """
    try:
        # Parse ISO timestamps
        target_start = datetime.fromisoformat(target_start_iso.replace("Z", "+00:00"))
        target_end = datetime.fromisoformat(target_end_iso.replace("Z", "+00:00"))
    except Exception:
        target_start = datetime.now(timezone.utc)
        target_end = target_start + timedelta(hours=1)

    window_start = target_start - timedelta(hours=window_hours)
    window_end = target_end + timedelta(hours=window_hours)

    all_events = get_calendar_events(window_start, window_end)

    conflicting = []
    surrounding = []

    for ev in all_events:
        try:
            ev_start = datetime.fromisoformat(ev["start_iso"].replace("Z", "+00:00"))
            ev_end = datetime.fromisoformat(ev["end_iso"].replace("Z", "+00:00"))
            # Direct overlap check: ev_start < target_end AND ev_end > target_start
            if ev_start < target_end and ev_end > target_start:
                conflicting.append(ev)
            else:
                surrounding.append(ev)
        except Exception:
            surrounding.append(ev)

    return conflicting, surrounding


# =====================================================================
# 3. Apple Reminders
# =====================================================================

def get_reminders(include_completed: bool = False, due_before: Optional[datetime] = None) -> List[Dict[str, Any]]:
    return native_store.get_reminders(include_completed, due_before)


def create_reminder(title: str, due_date_iso: Optional[str] = None, priority: int = 0, notes: str = "", list_name: Optional[str] = None) -> Dict[str, Any]:
    return _write(native_store.create_reminder, title, due_date_iso, priority, notes, list_name)


def complete_reminder(title_query: str) -> Dict[str, Any]:
    return _write(native_store.complete_reminder, title_query)


# =====================================================================
# 4. Apple Mail
# =====================================================================

def get_unread_emails(since_iso: Optional[str] = None, limit: int = 40) -> List[Dict[str, Any]]:
    return imap_mail.get_unread_emails(since_iso, limit)


def trash_email_by_id(message_id: str) -> Dict[str, Any]:
    return _write(imap_mail.trash_email_by_id, message_id)


def delete_emails(query: str) -> Dict[str, Any]:
    return _write(imap_mail.delete_emails, query)


def get_email_full_body(message_id: str) -> str:
    return imap_mail.get_email_full_body(message_id)


def create_draft_reply(recipient: str, subject: str, reply_body: str, original_message_id: Optional[str] = None) -> Dict[str, Any]:
    return _write(imap_mail.create_draft_reply, recipient, subject, reply_body, original_message_id)


def open_mail_app(message_id: Optional[str] = None, search_query: Optional[str] = None) -> bool:
    """
    Open/activate Apple Mail on the user's screen.
    If message_id is provided, opens that specific email message window.
    If search_query is provided, searches inbox and opens the matching message.
    Otherwise, activates Apple Mail and brings the main viewer to the front.
    """
    if message_id and str(message_id).startswith(("icloud:", "gmail:")):
        try:
            internet_id = imap_mail.get_message_id(message_id)
        except (RuntimeError, OSError, ValueError):
            return False
        script = '''
        on run argv
            set targetID to item 1 of argv
            tell application "Mail"
                activate
                set matches to (messages of inbox whose message id is targetID)
                if (count of matches) is not 1 then return "not_found"
                open (item 1 of matches)
                return "opened"
            end tell
        end run
        '''
        code, out, _ = _run_applescript(script, timeout=15, args=[internet_id.strip("<>")])
        return code == 0 and out == "opened"
    if message_id:
        try:
            int_id = int(message_id)
            script = f"""
            tell application "Mail"
                activate
                if (count of message viewers) is 0 then
                    make new message viewer
                end if
                try
                    set matches to (messages of inbox whose id is {int_id})
                    if (count of matches) is not 1 then return "not_found"
                    open (item 1 of matches)
                    return "opened"
                on error
                    return "not_found"
                end try
            end tell
            """
            code, out, _ = _run_applescript(script, timeout=10)
            return code == 0 and out == "opened"
        except (ValueError, TypeError):
            pass

    if search_query:
        safe_q = search_query.replace('\\', '\\\\').replace('"', '\\"')
        script = f"""
        tell application "Mail"
            activate
            if (count of message viewers) is 0 then
                make new message viewer
            end if
            try
                set theMsgs to (messages of inbox whose subject contains "{safe_q}" or sender contains "{safe_q}")
                if (count of theMsgs) > 0 then
                    open (item 1 of theMsgs)
                    return "opened"
                end if
                return "not_found"
            on error
                return "not_found"
            end try
        end tell
        """
        code, out, _ = _run_applescript(script, timeout=10)
        return code == 0 and out == "opened"

    # Default: bring Mail app to foreground and ensure viewer is open
    script = """
    tell application "Mail"
        activate
        if (count of message viewers) is 0 then
            make new message viewer
        end if
    end tell
    """
    code, _, _ = _run_applescript(script, timeout=10)
    return code == 0


# =====================================================================
# 5. Zero-Token Gatekeeper (Pre-filter for Emails)
# =====================================================================

# Senders that are automated, noreply, newsletters, promotions, or transactional
NOREPLY_SENDER_PATTERN = re.compile(
    r"(no[-_]?reply|donotreply|newsletter|marketing|billing|notifications?|"
    r"mailer|updates?|promo|automated|mailer-daemon|support@github|security@|"
    r"alerts?|system@|bounce|service@paypal|info@twitter|digest@|orders?@|"
    r"confirmations?@|news@|deals@|team@canva|hello@figma|verify@)",
    re.IGNORECASE
)

# Subjects that are purely informational, transactional, or automated
UNACTIONABLE_SUBJECT_PATTERN = re.compile(
    r"(unsubscribe|receipt|order confirmation|shipping confirmation|order received|"
    r"your invoice|statement available|password reset|security alert|verify your|"
    r"verification code|one-time code|2fa|login attempt|digest|weekly summary|"
    r"newsletter|what's new in|special offer|flash sale|discount|terms of service|"
    r"privacy policy update|survey|we value your feedback)",
    re.IGNORECASE
)

# Body snippet keywords that strongly indicate marketing or automated newsletters
UNACTIONABLE_SNIPPET_PATTERN = re.compile(
    r"(to unsubscribe|click here to unsubscribe|view this email in your browser|"
    r"manage email preferences|you received this email because|opt out|"
    r"all rights reserved|terms & conditions)",
    re.IGNORECASE
)


def is_actionable_email(sender: str, subject: str, snippet: str) -> bool:
    """
    Zero-Token Gatekeeper filter.
    Returns False if the email is automated, marketing, receipt, or newsletter.
    Returns True if it appears to be a personal, work, or actionable communication.
    """
    if NOREPLY_SENDER_PATTERN.search(sender):
        return False

    if UNACTIONABLE_SUBJECT_PATTERN.search(subject):
        return False

    if UNACTIONABLE_SNIPPET_PATTERN.search(snippet):
        return False

    # Empty subject and empty snippet is likely spam/empty ping
    if not subject.strip() and not snippet.strip():
        return False

    return True
