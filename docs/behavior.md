# How MacAssistant handles requests

## Mail

Gmail uses `imap.gmail.com:993`; iCloud uses `imap.mail.me.com:993`. Both connections use TLS with certificate verification. The app discovers Trash and Drafts through IMAP special-use flags, including localized Gmail folders.

Reading a message leaves it unread. Draft replies keep the original Message-ID and References headers and are appended to the server's Drafts folder. The app does not send mail. A recipient given only by name needs a single matching original message; otherwise, the request needs a full email address.

Each message reference includes its provider, account, mailbox UIDVALIDITY, and UID. Follow-ups such as "delete that one" use the last read message's reference. "Any others?" skips messages already read. A stale reference requires an inbox refresh. Changing accounts clears cached messages and conversational references. Older Apple Mail numeric IDs are not used for background operations.

Moving a message to Trash uses UID MOVE. If the server supports UIDPLUS, the app can also use UID COPY, STORE, and EXPUNGE for the selected message. It does not expunge unrelated messages. Connection and permission failures are reported as errors.

Mail, Calendar, and Reminders can stay closed during background work. An explicit request to open a message launches Apple Mail and requires that account to be configured there.

## Inbox checks and cleanup

Checks run on startup and at an interval of 5 to 120 minutes, with a default of 30 minutes. An unchanged scheduled check stops before reading bodies or making model requests. Manual checks speak a summary, including when it comes from cache.

Each check fetches up to 50 unread message headers. Cleanup uses `gpt-6-luna` through `/v1/decisions` to classify excerpts. An advertisement or temporary verification message can move to Trash only when the model's confidence is at least 95%. Uncertain results, refusals, endpoint failures, and failed moves leave the message in the inbox. Summaries can still run when the endpoint does not support Decisions.

Summary input includes each fetched message's sender, subject, and a body excerpt. Excerpts are limited to 1,500 characters per message and 24,000 characters in total. Those are character limits; token usage and cost depend on the model and the content.

## Calendar and Reminders

Both integrations use EventKit and the accounts synced on the Mac. Permission failures are reported as errors.

A calendar overview includes events that overlap the requested window, including ones that started earlier. It also includes reminders due in that window. Spoken schedules use local time and include duration, location, and all-day status. A reminder with only a due date is read without a midnight time.

Reminder questions fetch incomplete tasks. Without a date filter, that includes undated and future reminders. A follow-up that asks to read reminders does not create tasks from earlier email context.

Deleting a calendar event requires one matching occurrence. A date narrows the search; without one, the search covers the past year and the next three years. Completing a reminder requires one matching incomplete task. An ambiguous name or read-only calendar/list produces an error. The app does not silently choose another destination.

## Models and conversation

Text requests default to `gpt-5-nano` with `reasoning_effort="minimal"`. Configurations using the old `gpt-oss-20b` default migrate to nano. Transcription defaults to `gpt-4o-mini-transcribe` and can fall back to `whisper-1` if that model is unavailable.

Common schedule questions about today, tomorrow, or yesterday resolve locally and fetch fresh calendar or reminder data. Other dates, named lists, and complex filters use model extraction. Ordinary conversation uses one model call per answer and keeps a short history for follow-ups.

Short responses from non-reasoning models have a limit of 768 completion tokens. Nano, plans, drafts, and supported reasoning models use a limit of 2,048. Authentication errors, refusals, and outages do not trigger extra model fallbacks. Unsupported reasoning settings and unavailable-model fallbacks are remembered for the session. The API client uses a 30-second timeout and permits one transport retry.

## Caches

These caches are held in memory:

| Data | Maximum size | Lifetime |
| --- | --- | --- |
| Message bodies | 100 messages, 8,000 characters each | 30 minutes |
| Successful cleanup decisions | 500 content fingerprints | Until exit or eviction |
| Failed cleanup attempts | 500 content fingerprints | Five minutes before retry |
| Inbox summaries | 10 summaries | 30 minutes |
| Conversation | Three recent exchanges, 6,000 characters | Cleared after five idle minutes |

Logs may still contain account details from spoken responses. See [SECURITY.md](../SECURITY.md) before sharing them.

## Preferences, speech, and briefings

Saved preferences reload within two seconds while voice processing is idle, including shortcut and API-client changes. Muting speech stops playback. Starting another recording interrupts speech and discards late playback from the previous request.

Related successful actions share one spoken confirmation. Routine success banners are off by default; errors and calendar conflicts still appear. Menu bar icons adapt to light and dark appearance.

Only one morning briefing and one evening wrap-up can run at a time. A failed attempt waits five minutes before retrying. Missing weather and unknown completed-task counts are reported as unavailable. Morning briefings fetch weather from wttr.in.
