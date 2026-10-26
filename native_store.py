"""Calendar and reminder access through EventKit, without launching either app."""

import threading
from datetime import datetime, timedelta, timezone
from functools import wraps

import EventKit
import objc
from Foundation import NSDate, NSDateComponentUndefined, NSCalendar, NSCalendarUnitYear, NSCalendarUnitMonth, NSCalendarUnitDay, NSCalendarUnitHour, NSCalendarUnitMinute, NSCalendarUnitSecond


_lock = threading.RLock()
_store = None


def _native(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        with _lock, objc.autorelease_pool():
            return operation(*args, **kwargs)
    return run


def _wait(start, label):
    finished = threading.Event()
    result = []

    def complete(*values):
        result.extend(values)
        finished.set()

    start(complete)
    if not finished.wait(60):
        raise RuntimeError(f"{label} timed out. Check MacAssistant's permissions in System Settings.")
    return result


def _event_store(entity):
    global _store
    if _store is None:
        _store = EventKit.EKEventStore.alloc().init()
    status = EventKit.EKEventStore.authorizationStatusForEntityType_(entity)
    if status != EventKit.EKAuthorizationStatusFullAccess:
        label = "Calendar" if entity == EventKit.EKEntityTypeEvent else "Reminders"
        if status in (EventKit.EKAuthorizationStatusDenied, EventKit.EKAuthorizationStatusRestricted):
            raise PermissionError(f"Allow MacAssistant full {label} access in System Settings → Privacy & Security.")
        request = (_store.requestFullAccessToEventsWithCompletion_ if entity == EventKit.EKEntityTypeEvent
                   else _store.requestFullAccessToRemindersWithCompletion_)
        granted, error = _wait(request, f"{label} permission request")
        if not granted:
            raise PermissionError(str(error.localizedDescription()) if error else f"{label} access was denied.")
    return _store


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


def _iso(value):
    return datetime.fromtimestamp(value.timeIntervalSince1970(), timezone.utc).isoformat() if value else None


def _check(result):
    success, error = result
    if not success:
        raise RuntimeError(str(error.localizedDescription()) if error else "EventKit could not save the change.")


def _calendar(store, entity, name):
    if name:
        matches = [c for c in store.calendarsForEntityType_(entity) if c.title().casefold() == name.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Calendar/list '{name}' was not found uniquely.")
        calendar = matches[0]
    else:
        calendar = (store.defaultCalendarForNewEvents() if entity == EventKit.EKEntityTypeEvent
                    else store.defaultCalendarForNewReminders())
    if calendar is None or not calendar.allowsContentModifications():
        raise ValueError("No writable default calendar/list. Choose one in Calendar or Reminders settings.")
    return calendar


def _event_dict(event):
    return {"id": event.eventIdentifier(), "title": event.title() or "Untitled",
            "start_iso": _iso(event.startDate()), "end_iso": _iso(event.endDate()),
            "location": event.location() or "", "notes": event.notes() or "",
            "calendar": event.calendar().title(), "all_day": bool(event.isAllDay())}


def _events(store, start, end, calendars=None):
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end <= start:
        raise ValueError("Calendar end must be after start.")
    events = {}
    cursor = start
    # EventKit silently truncates a single predicate to four years.
    while cursor < end:
        stop = min(end, cursor + timedelta(days=365 * 3))
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(_date(cursor), _date(stop), calendars)
        for event in store.eventsMatchingPredicate_(predicate):
            events[(event.eventIdentifier(), _iso(event.startDate()))] = event
        cursor = stop
    return sorted(events.values(), key=lambda event: event.startDate().timeIntervalSince1970())


@_native
def get_calendar_events(start_dt, end_dt, calendar_name=None):
    store = _event_store(EventKit.EKEntityTypeEvent)
    calendars = None
    if calendar_name:
        calendars = [c for c in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
                     if c.title().casefold() == calendar_name.casefold()]
        if not calendars:
            raise ValueError(f"Calendar '{calendar_name}' was not found.")
    return [_event_dict(event) for event in _events(store, start_dt, end_dt, calendars)]


@_native
def create_calendar_event(title, start_iso, end_iso, location="", notes="", calendar_name=None):
    start, end = _date(start_iso), _date(end_iso)
    if end.timeIntervalSince1970() <= start.timeIntervalSince1970():
        raise ValueError("Calendar end must be after start.")
    store = _event_store(EventKit.EKEntityTypeEvent)
    event = EventKit.EKEvent.eventWithEventStore_(store)
    event.setCalendar_(_calendar(store, EventKit.EKEntityTypeEvent, calendar_name))
    event.setTitle_(title)
    event.setStartDate_(start)
    event.setEndDate_(end)
    event.setLocation_(location)
    event.setNotes_(notes)
    _check(store.saveEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None))
    return {"success": True, **_event_dict(event)}


@_native
def delete_calendar_event(title_query, start_iso=None, end_iso=None):
    query = title_query.strip().casefold()
    if not query:
        raise ValueError("No event title was provided.")
    if bool(start_iso) != bool(end_iso):
        raise ValueError("Specify both the start and end of the event's date window.")
    now = datetime.now().astimezone()
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else now - timedelta(days=365)
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else now + timedelta(days=365 * 3)
    store = _event_store(EventKit.EKEntityTypeEvent)
    matches = [event for event in _events(store, start, end) if query in (event.title() or "").casefold()]
    if len(matches) != 1:
        raise ValueError("No unique calendar event matches. Specify its title and date (default search: past year through next three years).")
    event = matches[0]
    result = _event_dict(event)
    _check(store.removeEvent_span_commit_error_(event, EventKit.EKSpanThisEvent, True, None))
    return {"success": True, **result}


def _reminder_dict(reminder):
    components = reminder.dueDateComponents()
    due = None
    if components:
        due = (components.calendar() or NSCalendar.currentCalendar()).dateFromComponents_(components)
    return {"id": reminder.calendarItemIdentifier(), "title": reminder.title() or "Untitled",
            "due_date": _iso(due), "priority": reminder.priority(), "body": reminder.notes() or "",
            "list": reminder.calendar().title(), "completed": bool(reminder.isCompleted()),
            "has_due_time": bool(components and components.hour() != NSDateComponentUndefined)}


def _reminders(store):
    predicate = store.predicateForRemindersInCalendars_(None)
    result, = _wait(lambda complete: store.fetchRemindersMatchingPredicate_completion_(predicate, complete), "Reminders query")
    if result is None:
        raise RuntimeError("EventKit could not read reminders.")
    return result


@_native
def get_reminders(include_completed=False, due_before=None):
    store = _event_store(EventKit.EKEntityTypeReminder)
    results = []
    for reminder in _reminders(store):
        if reminder.isCompleted() and not include_completed:
            continue
        data = _reminder_dict(reminder)
        if due_before and data["due_date"] and datetime.fromisoformat(data["due_date"]).timestamp() > due_before.timestamp():
            continue
        results.append(data)
    return results


@_native
def create_reminder(title, due_date_iso=None, priority=0, notes="", list_name=None):
    if not 0 <= priority <= 9:
        raise ValueError("Reminder priority must be 0–9.")
    store = _event_store(EventKit.EKEntityTypeReminder)
    reminder = EventKit.EKReminder.reminderWithEventStore_(store)
    reminder.setCalendar_(_calendar(store, EventKit.EKEntityTypeReminder, list_name))
    reminder.setTitle_(title)
    reminder.setNotes_(notes)
    reminder.setPriority_(priority)
    if due_date_iso:
        units = NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay | NSCalendarUnitHour | NSCalendarUnitMinute | NSCalendarUnitSecond
        calendar = NSCalendar.currentCalendar()
        components = calendar.components_fromDate_(units, _date(due_date_iso))
        components.setCalendar_(calendar)
        components.setTimeZone_(calendar.timeZone())
        reminder.setDueDateComponents_(components)
    _check(store.saveReminder_commit_error_(reminder, True, None))
    return {"success": True, **_reminder_dict(reminder)}


@_native
def complete_reminder(title_query):
    query = title_query.strip().casefold()
    if not query:
        raise ValueError("No reminder title was provided.")
    store = _event_store(EventKit.EKEntityTypeReminder)
    matches = [r for r in _reminders(store) if not r.isCompleted() and query in (r.title() or "").casefold()]
    if len(matches) != 1:
        raise ValueError("No unique incomplete reminder matches. Specify its full title or list.")
    reminder = matches[0]
    reminder.setCompleted_(True)
    _check(store.saveReminder_commit_error_(reminder, True, None))
    return {"success": True, **_reminder_dict(reminder)}
