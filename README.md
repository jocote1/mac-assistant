# MacAssistant

MacAssistant adds voice control for Gmail, iCloud Mail, Calendar, and Reminders to the macOS menu bar. Hold Cmd+5 to speak, then release it to send your request. You can ask about your schedule, add a reminder, read an email, or save a draft reply.

Mail and calendar tasks run in the background. Asking to open an email brings up Apple Mail. Transcription and text generation use the API endpoint you configure; speech playback runs locally.

## Install

Download the ZIP from the [latest release](https://github.com/jocote1/mac-assistant/releases/latest) for an Apple Silicon Mac running macOS 14 or later. Unzip it and move `MacAssistant.app` into `~/Applications` (create the folder if needed). Open the app, then enter your own API key and mail account in Preferences / Settings. Python and the app's dependencies are included.

The current download has an ad-hoc signature and is not notarized. macOS may block the first launch. If you trust the download, see [Apple's instructions for opening an unnotarized app](https://support.apple.com/en-us/102445). Managed Macs may prevent this.

### Run from source

You need macOS 14 or later and Python 3 with Tkinter. To check Tkinter, run `python3 -m tkinter`; it should open a test window.

```bash
git clone https://github.com/jocote1/mac-assistant.git
cd mac-assistant
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python main.py
```

Open Preferences / Settings from the menu bar. Enter your API key and base URL in API & Models. The defaults are `gpt-5-nano` for text and `gpt-4o-mini-transcribe` for transcription. You can also supply `OPENAI_API_KEY` and `OPENAI_BASE_URL` through your shell environment. The app does not load `.env` files.

To start it again from the checkout:

```bash
./run.sh
```

### Connect mail

MacAssistant connects to Gmail or iCloud Mail through IMAP. You need an app password for the account you want to use.

For Gmail, enable 2-Step Verification and [create an app password](https://myaccount.google.com/apppasswords). Some account policies prevent app passwords; [Google lists the restrictions](https://support.google.com/accounts/answer/185833).

For iCloud, create an app-specific password under Sign-In and Security at [Apple Account](https://account.apple.com/). Your Apple Account needs two-factor authentication. See [Apple's instructions](https://support.apple.com/en-us/102654).

1. Open Preferences / Settings, then the Mail tab.
2. Choose `gmail` or `icloud`, and enter your full email address and app password.
3. Click Test Connection, then Save Preferences.

Test Connection opens the inbox read-only. The password is saved in macOS Keychain. Leave the password field blank when updating other preferences to keep the saved password. To disconnect, click Remove Saved Password. Clear the address and save if you also want to remove the account from preferences.

Reading an email leaves its unread status unchanged. Replies are saved in Drafts and are never sent by the app. To open a message in Apple Mail, configure the same account there too.

### Allow macOS access

Allow microphone access when prompted. Calendar and Reminders use the accounts already synced on your Mac and need full access under System Settings > Privacy & Security. Check that those accounts are enabled in Internet Accounts or iCloud settings.

Cmd+5 uses a native shortcut. Single-modifier shortcuts, such as Right Option, use an Accessibility listener and need Accessibility permission. Opening an email in Apple Mail may also require Automation permission.

## Use it

Hold Cmd+5 while speaking. Release it to process the request. Press it again to interrupt speech and begin another request. You can also record from the menu bar.

Try:

- "What's on my calendar tomorrow?"
- "Add a meeting with Sarah on Monday at three."
- "Remind me to prepare the slides tomorrow morning."
- "Summarize my inbox."
- "Read the first email." Then: "Delete that one."
- "Draft a reply to Sam asking about the deadline."

When a name or reference matches several items, the app asks which one you mean. For a draft addressed only by name, it needs a single matching original email; otherwise, give the full address.

Inbox checks run at startup and every 30 minutes by default. Change the interval, morning briefing time, and evening wrap-up time in preferences. Scheduled inbox summaries stay quiet when nothing has changed. Manual checks speak even when the summary is cached.

Inbox cleanup can move advertisements and temporary verification messages to Trash automatically. It requires a Decisions API endpoint and a classification confidence of at least 95%. Messages with uncertain classifications stay in the inbox. See [how the app handles requests](docs/behavior.md) for the limits and error handling.

### Speech and notifications

Speech uses macOS `say` unless Kokoro-82M weights are installed. To download them, run this from the project directory:

```bash
.venv/bin/python -c 'from tts_engine import download_kokoro_weights; download_kokoro_weights()'
```

The weights are stored in `~/.mac_assistant/models/`. Choose the voice and speed in preferences. Turning speech off stops current playback. Routine success notifications are off by default; you can enable them in Voice & PTT. Errors and calendar conflicts still produce notifications.

## Your data

Preferences, including the API key, are stored in `~/.mac_assistant/config.json`. The key is stored as plain text there. Mail app passwords are stored separately in macOS Keychain. State, logs, and downloaded models also live under `~/.mac_assistant/`.

Audio goes to the configured transcription endpoint. Text requests can include email excerpts, calendar details, reminders, and recent conversation. Morning briefings also fetch weather from wttr.in. Logs can contain spoken responses and other account details.

Personal settings and runtime files are excluded from Git. See [SECURITY.md](SECURITY.md) for what to keep out of commits and what to do if a credential is exposed.

## Build a standalone app

On an Apple Silicon Mac, with the virtual environment above installed:

```bash
.venv/bin/python -m pip install -r packaging/requirements-build.txt
.venv/bin/python packaging/generate_icons.py
.venv/bin/python -m PyInstaller --noconfirm MacAssistant.spec
```

The build produces `dist/MacAssistant.app` with an ad-hoc signature. It is not notarized. Quit any running copy before installing a new build:

```bash
mkdir -p ~/Applications
ditto dist/MacAssistant.app ~/Applications/MacAssistant.app
open ~/Applications/MacAssistant.app
```

Launch at Login uses the copy in `~/Applications/MacAssistant.app`. Packaged runs write logs to `~/.mac_assistant/assistant.log`; View Logs opens that file in Console.

To check bundled imports and local speech-model loading without recording, playback, inbox checks, or API calls:

```bash
dist/MacAssistant.app/Contents/MacOS/MacAssistant --self-check
```

## Development

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

The tests mock API requests, native account operations, keyboard registration, and audio hardware. Configuration tests use temporary directories.

[CONTRIBUTING.md](CONTRIBUTING.md) covers the code layout and checks for changes. [docs/behavior.md](docs/behavior.md) describes message references, model requests, and cache limits.
