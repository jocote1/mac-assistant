# Contributing

Follow the [installation instructions](README.md#install) to set up a virtual environment on macOS. The app uses Cocoa, EventKit, and Security frameworks, so native integrations need a Mac.

## Code layout

| File | What it does |
| --- | --- |
| `main.py` | Menu bar, recording, shortcuts, and scheduled checks |
| `ai_pipeline.py` | Transcription, request planning, conversation, and inbox summaries |
| `macos_bridge.py` | Connects requests to native operations, notifications, and Apple Mail |
| `native_store.py` | Reads and updates Calendar and Reminders through EventKit |
| `imap_mail.py` | Reads mail, moves messages to Trash, and saves drafts over IMAP |
| `mail_credentials.py` | Stores mail app passwords in Keychain |
| `config.py` | Preferences, saved state, and login registration |
| `tts_engine.py` | Kokoro speech synthesis, playback, and the macOS `say` fallback |
| `global_hotkey.py` | Registers the native push-to-talk shortcut |
| `assets/` | App icon and menu bar icons |
| `packaging/generate_icons.py` | Generates icons with Cocoa |
| `MacAssistant.spec` | Builds the Apple Silicon app bundle with an ad-hoc signature |

The [behavior notes](docs/behavior.md) describe request limits and how the app tracks messages.

## Check your changes

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Tests should mock API requests, Keychain, account access, audio devices, keyboard registration, and login services. Use temporary directories for saved configuration. Fixtures should contain made-up messages and addresses such as `person@example.com`; do not copy an inbox or log into a test.

If you change packaging, [build the app](README.md#build-a-standalone-app) and run its `--self-check` command too.

Before committing, review `git diff --cached` for credentials and personal data. [SECURITY.md](SECURITY.md) explains the storage locations and ignore rules. In a pull request, describe what changes for the user and how you checked it.
