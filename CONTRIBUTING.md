# Contributing

## Local setup

Follow the installation steps in [README.md](README.md). Use macOS and a project-local virtual environment; native integrations depend on Cocoa, EventKit, and Security frameworks.

## Validation

Run the existing regression suite before submitting changes:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Mock API requests, Keychain operations, native account access, audio devices, and login registration in tests. Use synthetic messages and accounts such as `person@example.com`. Do not copy personal inbox contents or logs into fixtures.

For changes to packaging, also build `MacAssistant.spec` and run the packaged `--self-check` described in the README.

## Before committing

Review staged files with `git diff --cached` and follow [SECURITY.md](SECURITY.md). Keep credentials, personal settings, generated bundles, downloaded models, and virtual environments out of commits. Describe the behavior change and validation in your pull request.
