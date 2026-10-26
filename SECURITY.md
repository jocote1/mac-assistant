# Security and private data

Never commit API keys, mail app passwords, personal configuration, logs, recordings, or account data, even to a private repository.

MacAssistant stores preferences and its API key in `~/.mac_assistant/config.json`. Runtime state, logs, and downloaded speech models also live under `~/.mac_assistant/`. Mail app passwords are stored in macOS Keychain. These files and Keychain items are not part of this repository. Environment variables `OPENAI_API_KEY` and `OPENAI_BASE_URL` can supply API configuration; the app does not load `.env` files automatically.

The `.gitignore` excludes local configuration, environment files, credentials, recordings, logs, model weights, virtual environments, and generated app bundles. Ignore rules do not protect secrets pasted into source files or files already tracked by Git. Review `git status --short` and `git diff --cached` before every push.

Voice transcription and model requests send relevant audio, prompts, email excerpts, and task context to the configured API endpoint. Mail access connects to the selected provider over IMAP with TLS. Calendar and Reminders access uses macOS EventKit. This repository contains application code and synthetic test fixtures, not an export of any account.

If a credential is accidentally committed, revoke or rotate it immediately, then remove it from Git history. Deleting the working-tree file alone does not remove previous commits.

Do not put credentials, private mail, or personal logs in public issues. Report suspected vulnerabilities privately to the repository owner, using a minimal reproduction with synthetic data.
