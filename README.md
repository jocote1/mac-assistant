# MacAssistant

A macOS menu bar voice assistant for Gmail, iCloud Mail, Calendar, and Reminders. Hold **Cmd+5** to speak; release to submit. Press again to interrupt speech and start a new request.

## Requirements

- macOS 14 or later. The standalone build targets Apple Silicon (`arm64`).
- Python 3 with Tkinter available (`python3 -m tkinter` opens a test window).
- An API key and endpoint supporting the configured transcription and text models.
- A Gmail or iCloud app password for optional mail access, plus synced macOS accounts for Calendar and Reminders.

## Install from GitHub

```bash
git clone https://github.com/jocote1/mac-assistant.git
cd mac-assistant
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Configure your own API key and mail account through **Preferences / Settings…**. The repository includes no personal configuration or credentials.

## Privacy and repository contents

Personal preferences, the API key, runtime state, logs, and downloaded models live under `~/.mac_assistant/`, outside this checkout. Mail app passwords live in macOS Keychain. Git excludes local settings, `.env` files, credentials, recordings, logs, virtual environments, and generated builds.

Transcription and model requests send relevant audio and task context, including email excerpts when used, to your configured API endpoint. Review [SECURITY.md](SECURITY.md) for storage and credential handling and [CONTRIBUTING.md](CONTRIBUTING.md) for development checks.

## Background access in 1.2

Mail checks, body reads, Trash moves, and unsent drafts connect directly to Gmail or iCloud over encrypted IMAP. Calendar and Reminders use EventKit and the accounts already synced on your Mac. These operations do not launch Mail, Calendar, or Reminders, create their windows, or add their running icons to the Dock. Explicit requests to open an email still open Apple Mail; that account must also be configured in Mail.

Set up the rebuilt app once:

1. For Gmail, enable Google 2-Step Verification and [create an app password](https://myaccount.google.com/apppasswords), named MacAssistant. This uses IMAP without an OAuth client or Google Cloud project. Google may disable app passwords for accounts with Advanced Protection or other restrictions; see [Google's requirements](https://support.google.com/accounts/answer/185833). For iCloud, generate an [app-specific password](https://support.apple.com/en-us/102654) at [Apple Account](https://account.apple.com/) → Sign-In and Security → App-Specific Passwords, with two-factor authentication enabled.
2. Open **Preferences → Mail**, choose **gmail** (the default) or **icloud**, enter your full email address and app password, then **Test Connection** and **Save Preferences**. **Create App Password…** opens the selected provider's account page. Spaces in Google's grouped app password are removed automatically. Passwords are stored separately for each provider/account in macOS Keychain; only the provider and email address are saved in JSON. Leave the password field blank on later visits to keep the saved password. **Remove Saved Password** disconnects mail access; clear the address and save to remove the configured account too. Existing iCloud settings and Keychain passwords continue to work.
3. Allow MacAssistant full Calendar and Reminders access when macOS prompts. Manage access in **System Settings → Privacy & Security → Calendars / Reminders**. Ensure the desired accounts are enabled in macOS Internet Accounts/iCloud settings. No separate calendar credentials are needed.

IMAP uses [Gmail's server](https://developers.google.com/workspace/gmail/imap/imap-smtp), `imap.gmail.com:993`, or [iCloud's server](https://support.apple.com/en-us/102525), `imap.mail.me.com:993`, with TLS certificate verification. Reading messages preserves unread status. Drafts are appended to the server's Drafts folder and never sent. Trash and Drafts folders are discovered using special-use flags, including Gmail's localized folders. Replies retain the original Message-ID and References headers. A recipient specified only by name requires a uniquely identified original email; otherwise give the recipient's full address. **Test Connection** only signs in and opens the inbox read-only.

Message references include the provider, account identity, mailbox UIDVALIDITY, and UID. Stale references fail and ask for an inbox refresh. Switching accounts clears cached mail content and conversation references. Trash operations use UID MOVE, or UID COPY/STORE/EXPUNGE when UIDPLUS is supported; they never expunge unrelated messages. A failed connection or denied permission reports an error rather than an empty inbox/calendar. Old references from Apple Mail's local numeric IDs are not reused for background actions.

Calendar deletion requires exactly one matching occurrence. Specify the date to narrow the search; without a date it searches the past year through the next three years. Reminder completion also requires exactly one matching incomplete reminder. Read-only or ambiguously named destination calendars/lists report errors instead of choosing another destination.

## What's new in 1.1

- Reminder questions read live incomplete tasks, including undated reminders when no date is requested. Read-only reminder follow-ups cannot create reminders from email context.
- Calendar overviews include reminders due in the same window. Spoken schedules include event duration, location, all-day status, and natural clock times; date-only reminders do not announce midnight.
- Email follow-ups retain actual message IDs: “delete that one” targets the last read email, combined sender/subject references resolve correctly, and “any others?” skips messages already read. Deleted IDs leave conversational context; ambiguous references ask which email.
- Natural conversation and follow-ups use bounded recent context. Ordinary conversational answers need one model call instead of separate routing and answering calls.
- Related successful actions share one confirmation. Routine success banners default off; errors and calendar conflicts still surface. Enable success banners in **Preferences → Voice & PTT**.
- A custom app logo and monochrome menu glyphs replace emoji. Template icons adapt to light and dark menu bars.
- Unchanged scheduled inbox checks stop before body reads or model calls. Repeated manual summaries reuse in-memory body, classification, and summary caches.
- Failed Calendar, Reminders, Mail, and draft operations report the native failure. Calendar overlap queries include events that started before the search window, and displayed times use the local timezone.
- Preferences reload within two seconds while voice processing is idle, including the hotkey and API client. Microphone failures recover cleanly. Muting speech stops current playback.
- Briefing workers cannot duplicate themselves while running. Failed briefing attempts wait five minutes before retrying. Missing weather stays unavailable; unknown completed-task counts stay unknown.

## Run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Open **Preferences / Settings…** from the menu bar to set your API key, endpoint, models, voice, shortcut, and schedule. Settings live in `~/.mac_assistant/config.json`. `OPENAI_API_KEY` and `OPENAI_BASE_URL` environment variables are supported.

The main text model is `gpt-5-nano`, with explicit minimal reasoning for faster voice responses. Older configurations using the removed OSS default migrate to nano automatically. Common calendar and reminder questions for today, tomorrow, or yesterday resolve locally without text-model requests; native data is still fetched fresh. Other dates, named lists, and complex filters use structured model extraction. Transcription defaults to `gpt-4o-mini-transcribe`, with `whisper-1` as an availability fallback. Spoken output uses local Kokoro-82M when its weights are installed, otherwise macOS `say`.

Examples:

- “What's on my calendar tomorrow?”
- “Add a meeting with Sarah on Monday at three, then remind me to prepare the slides.”
- “Summarize my inbox.”
- “Read the first email.” / “Open the second one.”
- “Draft a reply to Sam asking about the deadline.”
- “What's Austria's capital?” / “Tell me more about it.”

The native Cmd+5 shortcut does not monitor arbitrary keyboard input. Single modifier shortcuts such as Right Option use the Accessibility listener. Microphone, full Calendar/Reminders access, and Automation permission for explicitly opening Mail may require macOS permission. Recording is also available from the menu.

## Inbox checks and token budgets

Scheduled checks run on startup and at the configured interval (5–120 minutes, default 30). A changed inbox is summarized aloud; unchanged checks stay quiet. Manual checks always speak, including cached summaries.

Each check reads up to 50 unread message headers. Disposable-message cleanup uses the Decisions API (`gpt-6-luna`) to classify excerpts. Only obvious advertisements or temporary verification messages with at least 95% confidence move to Trash by exact ID. Uncertain results, refusals, failed deletions, and endpoint errors retain the message. Your endpoint must support `/v1/decisions` for this cleanup; summary generation can still run when it does not.

Caches remain in memory:

| Data | Limit | Reuse |
| --- | --- | --- |
| Mail bodies | 100 messages, 8,000 characters each | 30 minutes |
| Successful cleanup classifications | 500 evidence fingerprints | Until app exit or cache eviction |
| Failed cleanup attempts | 500 fingerprints | Five-minute retry cooldown |
| Inbox summaries | 10 summaries | 30 minutes |
| Conversation | Three recent exchanges, 6,000 characters | Five-minute idle timeout |

Summary input includes sender, subject, and a body excerpt from every fetched message. The total body-excerpt budget is 24,000 characters, with at most 1,500 per message. For 50 messages, this reduces body input from 75,000 to 24,000 characters (68%). Character savings are not a measured token or cost percentage.

Structured generation has output limits: 768 completion tokens for short responses on non-reasoning models; 2,048 for nano, plans, drafts, and supported reasoning models. Nano uses `reasoning_effort="minimal"`. Authentication failures, refusals, and outages do not trigger extra application-level model fallbacks. Unsupported reasoning settings and unavailable-model fallbacks are remembered for the session. The SDK permits one transport retry and uses a 30-second request timeout.

## Build the standalone app

On an Apple Silicon Mac:

```bash
.venv/bin/python -m pip install -r packaging/requirements-build.txt
.venv/bin/python packaging/generate_icons.py
.venv/bin/python -m PyInstaller --noconfirm MacAssistant.spec
```

The app is `dist/MacAssistant.app`. Quit any running copy before opening a new build. To update the installed copy:

```bash
ditto dist/MacAssistant.app ~/Applications/MacAssistant.app
open ~/Applications/MacAssistant.app
```

**Launch at Login** uses the installed app at `~/Applications/MacAssistant.app`. Logs for packaged runs go to `~/.mac_assistant/assistant.log`; **View Logs…** opens them in Console.

Run an import and local speech-model check without microphone capture, playback, inbox checks, or paid API calls:

```bash
dist/MacAssistant.app/Contents/MacOS/MacAssistant --self-check
```

Kokoro weights live in `~/.mac_assistant/models/`. Download them with:

```python
from tts_engine import download_kokoro_weights
download_kokoro_weights()
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -q
```

The suite mocks paid APIs, native app operations, keyboard registration, and audio hardware. Configuration tests use temporary directories. Tests do not change your Mail, Calendar, Reminders, login items, or assistant state.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Menu bar, main-loop UI dispatch, recorder, hotkeys, scheduler |
| `ai_pipeline.py` | Conversation, planning, structured extraction, native outcomes, inbox caches |
| `macos_bridge.py` | Background integration facade, notifications, and explicitly opening Mail |
| `native_store.py` | EventKit access to synced calendars and reminders |
| `imap_mail.py` | Direct Gmail/iCloud IMAP reads, exact-message Trash moves, and unsent drafts |
| `mail_credentials.py` | Mail app password storage in macOS Keychain |
| `config.py` | Settings, validation, state, preferences window, login registration |
| `tts_engine.py` | Interruptible local Kokoro speech and native `say` fallback |
| `global_hotkey.py` | Native press/release shortcut registration |
| `assets/` | Bundled app icon and template menu glyphs |
| `packaging/generate_icons.py` | Reproducible Cocoa icon generator |
| `MacAssistant.spec` | Signed local PyInstaller app bundle configuration |
