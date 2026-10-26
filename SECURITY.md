# Security and private data

## Storage

Preferences and the API key are saved in plain text in `~/.mac_assistant/config.json`. Mail app passwords are stored in macOS Keychain, separately for each provider and account. Runtime state, logs, and downloaded speech models are also stored under `~/.mac_assistant/`.

`OPENAI_API_KEY` and `OPENAI_BASE_URL` can supply API configuration. The app does not read `.env` files automatically. Saving preferences can write the active API key to the configuration file, including a key originally supplied through the environment.

Mail body caches and conversation history are held in memory. Logs can contain spoken summaries, message subjects, and other account details. Review logs before sharing them.

## Network requests

Recorded audio is sent to the configured transcription endpoint. Text-model requests can include prompts, email excerpts, calendar entries, reminders, and recent conversation. Choose an endpoint you trust with that data.

Mail connects to Gmail or iCloud over IMAP with TLS certificate verification. Calendar and Reminders use macOS EventKit and the accounts synced on the Mac. Morning briefings request weather from wttr.in. Downloading Kokoro weights contacts GitHub.

## What belongs in Git

Keep API keys, mail passwords, account exports, personal settings, logs, and recordings out of commits. Use made-up messages and `example.com` addresses in tests.

The `.gitignore` covers local settings, environment files, common credential files, recordings, logs, model weights, virtual environments, and generated app bundles. It cannot protect a secret pasted into source code or a file Git already tracks. Before pushing, review:

```bash
git status --short
git diff --cached
```

If a credential reaches a commit, revoke or rotate it first. Then remove it from Git history. Deleting the current file leaves the credential in earlier commits.

## Reporting a problem

Contact the repository owner privately about a suspected vulnerability. Include steps to reproduce it with made-up data. Do not attach credentials, private email, or unredacted logs to an issue.
