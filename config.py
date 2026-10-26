"""Preferences, saved state, and launch-at-login registration for MacAssistant.

Stores configuration under ~/.mac_assistant/ and provides the Tkinter settings window.
"""

import json
import plistlib
import os
import subprocess
import sys
import re

# Auto-re-execute inside local .venv if run with system/conda python
_project_dir = os.path.dirname(os.path.abspath(__file__))
_venv_dir = os.path.join(_project_dir, ".venv")
_venv_python = os.path.join(_venv_dir, "bin", "python")

if not getattr(sys, "frozen", False) and os.path.exists(_venv_python) and os.path.abspath(sys.prefix) != os.path.abspath(_venv_dir):
    os.execv(_venv_python, [_venv_python] + sys.argv)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ASSISTANT_DIR = Path.home() / ".mac_assistant"
CONFIG_FILE = ASSISTANT_DIR / "config.json"
STATE_FILE = ASSISTANT_DIR / "state.json"
MODELS_DIR = ASSISTANT_DIR / "models"
LOG_FILE = ASSISTANT_DIR / "assistant.log"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_FILE = LAUNCH_AGENTS_DIR / "com.user.macassistant.plist"

DEFAULT_CONFIG: Dict[str, Any] = {
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "text_model": "gpt-5-nano",
    "stt_model": "gpt-4o-mini-transcribe",
    "ptt_hotkey": "cmd+5",  # Options: cmd+5, alt_r, cmd_r, ctrl+space, f6, space
    "poll_interval_minutes": 30,
    "morning_briefing_time": "08:00",
    "evening_wrapup_time": "20:00",
    "tts_voice": "af_heart",  # af_heart, am_michael, af_bella, am_adam, etc.
    "tts_speed": 1.0,
    "tts_enabled": True,
    "action_notifications": False,
    "launch_at_login": False,
    "auto_conflict_resolution": True,
    "mail_provider": "gmail",
    "mail_email": "",
}

DEFAULT_STATE: Dict[str, Any] = {
    "last_email_check": None,
    "last_morning_briefing_date": None,
    "last_evening_wrapup_date": None,
    "processed_email_ids": [],
}


def ensure_directories():
    """Ensure assistant directories exist."""
    ASSISTANT_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)


class AppConfig:
    """Handles loading, saving, and querying user configuration."""

    def __init__(self):
        ensure_directories()
        self.config: Dict[str, Any] = self.load()

    def load(self) -> Dict[str, Any]:
        """Load configuration from disk, filling in missing defaults."""
        data = dict(DEFAULT_CONFIG)
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    data.update(saved)
                    if "mail_provider" not in saved and saved.get("icloud_email"):
                        data["mail_provider"] = "icloud"
                        data["mail_email"] = saved["icloud_email"]
            except Exception as e:
                print(f"[Config] Error reading {CONFIG_FILE}: {e}", file=sys.stderr)

        if data.get("text_model") == "gpt-oss-20b":
            data["text_model"] = "gpt-5-nano"

        # Fallback to environment variable for API Key if not set in config
        env_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not data.get("openai_api_key") and env_key:
            data["openai_api_key"] = env_key

        env_base = os.environ.get("OPENAI_BASE_URL", "").strip()
        if env_base and data.get("openai_base_url") == DEFAULT_CONFIG["openai_base_url"]:
            data["openai_base_url"] = env_base

        return data

    def save(self):
        """Save configuration to disk."""
        ensure_directories()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"[Config] Error saving {CONFIG_FILE}: {e}", file=sys.stderr)

    def reload(self) -> bool:
        """Apply preferences saved by the separate settings process."""
        data = self.load()
        if data == self.config:
            return False
        self.config = data
        return True

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        self.config[key] = value
        self.save()

    @property
    def openai_api_key(self) -> str:
        return self.config.get("openai_api_key", "").strip()

    @property
    def openai_base_url(self) -> str:
        return self.config.get("openai_base_url", "https://api.openai.com/v1").strip()

    @property
    def text_model(self) -> str:
        return self.config.get("text_model", "gpt-5-nano")

    @property
    def stt_model(self) -> str:
        return self.config.get("stt_model", "gpt-4o-mini-transcribe")

    @property
    def ptt_hotkey(self) -> str:
        return self.config.get("ptt_hotkey", "cmd+5")

    @property
    def poll_interval_minutes(self) -> int:
        try:
            return max(5, min(120, int(self.config.get("poll_interval_minutes", 30))))
        except (TypeError, ValueError):
            return 30

    @property
    def morning_briefing_time(self) -> str:
        return self._valid_time("morning_briefing_time", "08:00")

    @property
    def evening_wrapup_time(self) -> str:
        return self._valid_time("evening_wrapup_time", "20:00")

    def _valid_time(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return value if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) else default

    @property
    def tts_voice(self) -> str:
        return self.config.get("tts_voice", "af_heart")

    @property
    def tts_speed(self) -> float:
        return float(self.config.get("tts_speed", 1.0))

    @property
    def tts_enabled(self) -> bool:
        return bool(self.config.get("tts_enabled", True))

    @property
    def launch_at_login(self) -> bool:
        return bool(self.config.get("launch_at_login", False))

    def update_launch_at_login(self, enable: bool) -> bool:
        """Create or remove macOS LaunchAgent plist file and register with launchctl."""
        installed_app = Path.home() / "Applications" / "MacAssistant.app"
        if getattr(sys, "frozen", False):
            installed_app = Path(sys.executable).parents[2]
        if installed_app.is_dir():
            arguments = ["/usr/bin/open", "-g", str(installed_app)]
            work_dir = str(installed_app.parent)
        else:
            arguments = [sys.executable, str(Path(__file__).resolve().with_name("main.py"))]
            work_dir = str(Path(__file__).resolve().parent)
        service = f"gui/{os.getuid()}/com.user.macassistant"
        try:
            subprocess.run(["launchctl", "bootout", service], capture_output=True)
            if enable:
                LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
                with PLIST_FILE.open("wb") as file:
                    plistlib.dump({
                        "Label": "com.user.macassistant", "ProgramArguments": arguments,
                        "RunAtLoad": True, "KeepAlive": False,
                        "StandardOutPath": str(LOG_FILE), "StandardErrorPath": str(LOG_FILE),
                        "WorkingDirectory": work_dir,
                    }, file)
                result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_FILE)], capture_output=True, text=True)
                if result.returncode:
                    print(f"[Config] Login registration failed: {result.stderr.strip()}", file=sys.stderr)
                    return False
            else:
                PLIST_FILE.unlink(missing_ok=True)
            self.config["launch_at_login"] = enable
            self.save()
            return True
        except Exception as error:
            print(f"[Config] Error setting launch at login: {error}", file=sys.stderr)
            return False


class AppState:
    """Handles transient and recurring operational state in ~/.mac_assistant/state.json."""

    def __init__(self):
        ensure_directories()
        self.state: Dict[str, Any] = self.load()

    def load(self) -> Dict[str, Any]:
        data = dict(DEFAULT_STATE)
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    data.update(saved)
            except Exception as e:
                print(f"[State] Error reading {STATE_FILE}: {e}", file=sys.stderr)
        return data

    def save(self):
        ensure_directories()
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[State] Error saving {STATE_FILE}: {e}", file=sys.stderr)

    @property
    def last_email_check(self) -> Optional[str]:
        return self.state.get("last_email_check")

    def record_email_check(self, timestamp_iso: Optional[str] = None):
        if not timestamp_iso:
            timestamp_iso = datetime.now(timezone.utc).isoformat()
        self.state["last_email_check"] = timestamp_iso
        self.save()

    @property
    def last_morning_briefing_date(self) -> Optional[str]:
        return self.state.get("last_morning_briefing_date")

    def record_morning_briefing(self, date_str: Optional[str] = None):
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        self.state["last_morning_briefing_date"] = date_str
        self.save()

    @property
    def last_evening_wrapup_date(self) -> Optional[str]:
        return self.state.get("last_evening_wrapup_date")

    def record_evening_wrapup(self, date_str: Optional[str] = None):
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        self.state["last_evening_wrapup_date"] = date_str
        self.save()

    def is_email_processed(self, email_id: str) -> bool:
        return email_id in self.state.get("processed_email_ids", [])

    def mark_email_processed(self, email_id: str):
        ids: List[str] = self.state.setdefault("processed_email_ids", [])
        if email_id not in ids:
            ids.append(email_id)
            # Keep only the last 500 email IDs to prevent indefinite growth
            if len(ids) > 500:
                self.state["processed_email_ids"] = ids[-500:]
            self.save()


def launch_settings_gui():
    """Launch the Tkinter Preferences / Settings GUI in a standalone process."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    import threading
    import queue
    from mail_credentials import get_password, save_password, delete_password
    from imap_mail import test_connection, validate_account

    cfg = AppConfig()

    root = tk.Tk()
    root.title("MacAssistant Preferences")
    root.geometry("600x760")
    root.resizable(False, False)

    # Style
    style = ttk.Style()
    try:
        style.theme_use("aqua")
    except Exception:
        pass

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=15, pady=15)

    # --- TAB 1: API & Models ---
    tab_api = ttk.Frame(notebook, padding=15)
    notebook.add(tab_api, text="API & Models")

    ttk.Label(tab_api, text="OpenAI API Key:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    api_key_var = tk.StringVar(value=cfg.openai_api_key)
    api_key_entry = ttk.Entry(tab_api, textvariable=api_key_var, show="•", width=50)
    api_key_entry.pack(anchor="w", fill="x", pady=(0, 5))

    show_key_var = tk.BooleanVar(value=False)

    def toggle_show_key():
        api_key_entry.config(show="" if show_key_var.get() else "•")

    ttk.Checkbutton(tab_api, text="Show API Key", variable=show_key_var, command=toggle_show_key).pack(anchor="w", pady=(0, 10))

    ttk.Label(tab_api, text="OpenAI Base URL:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    base_url_var = tk.StringVar(value=cfg.openai_base_url)
    ttk.Entry(tab_api, textvariable=base_url_var, width=50).pack(anchor="w", fill="x", pady=(0, 15))

    ttk.Label(tab_api, text="Text Model:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    text_model_var = tk.StringVar(value=cfg.text_model)
    text_model_combo = ttk.Combobox(
        tab_api,
        textvariable=text_model_var,
        values=["gpt-5-nano", "gpt-4.1-nano", "gpt-4o-mini", "gpt-4o"],
        state="readonly",
    )
    text_model_combo.pack(anchor="w", fill="x", pady=(0, 5))
    ttk.Label(tab_api, text="GPT-5 nano uses minimal reasoning for faster voice responses.", font=("Helvetica", 10), foreground="#666").pack(anchor="w", pady=(0, 15))

    ttk.Label(tab_api, text="Speech-to-Text Model:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    stt_model_var = tk.StringVar(value=cfg.stt_model)
    stt_model_combo = ttk.Combobox(
        tab_api,
        textvariable=stt_model_var,
        values=["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
        state="readonly",
    )
    stt_model_combo.pack(anchor="w", fill="x", pady=(0, 15))

    # --- TAB 2: Voice & Push-to-Talk ---
    tab_voice = ttk.Frame(notebook, padding=15)
    notebook.add(tab_voice, text="Voice & PTT")

    ttk.Label(tab_voice, text="Global Push-to-Talk Hotkey:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    hotkey_var = tk.StringVar(value=cfg.ptt_hotkey)
    hotkey_combo = ttk.Combobox(
        tab_voice,
        textvariable=hotkey_var,
        values=["cmd+5", "alt_r", "cmd_r", "ctrl+space", "f6", "space"],
        state="readonly",
    )
    hotkey_combo.pack(anchor="w", fill="x", pady=(0, 5))
    ttk.Label(tab_voice, text="Hold the key while speaking; release to execute.", font=("Helvetica", 10), foreground="#666").pack(anchor="w", pady=(0, 15))

    tts_enabled_var = tk.BooleanVar(value=cfg.tts_enabled)
    ttk.Checkbutton(tab_voice, text="Enable Spoken Audio (Kokoro TTS / macOS say)", variable=tts_enabled_var).pack(anchor="w", pady=(0, 10))
    action_notifications_var = tk.BooleanVar(value=cfg.get("action_notifications", False))
    ttk.Checkbutton(tab_voice, text="Show success banners for voice actions", variable=action_notifications_var).pack(anchor="w", pady=(0, 10))

    ttk.Label(tab_voice, text="Kokoro Voice Preset:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    voice_var = tk.StringVar(value=cfg.tts_voice)
    voice_combo = ttk.Combobox(
        tab_voice,
        textvariable=voice_var,
        values=["af_heart", "am_michael", "af_bella", "af_sarah", "am_adam", "bm_george", "bf_emma"],
        state="readonly",
    )
    voice_combo.pack(anchor="w", fill="x", pady=(0, 15))

    ttk.Label(tab_voice, text="Speech Speed Factor:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    speed_var = tk.DoubleVar(value=cfg.tts_speed)
    speed_scale = ttk.Scale(tab_voice, from_=0.8, to=1.4, variable=speed_var, orient="horizontal")
    speed_scale.pack(anchor="w", fill="x", pady=(0, 5))

    speed_lbl = ttk.Label(tab_voice, text=f"{cfg.tts_speed:.2f}x")
    speed_lbl.pack(anchor="w", pady=(0, 15))

    def update_speed_lbl(val):
        speed_lbl.config(text=f"{float(val):.2f}x")

    speed_scale.config(command=update_speed_lbl)

    # --- TAB 3: Automations & System ---
    tab_auto = ttk.Frame(notebook, padding=15)
    notebook.add(tab_auto, text="Automations")

    ttk.Label(tab_auto, text="Inbox Check Interval (Minutes):", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    poll_var = tk.IntVar(value=cfg.poll_interval_minutes)
    poll_spin = ttk.Spinbox(tab_auto, from_=5, to=120, increment=5, textvariable=poll_var, width=10)
    poll_spin.pack(anchor="w", pady=(0, 10))

    ttk.Label(tab_auto, text="Morning Briefing Time (HH:MM):", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    morning_var = tk.StringVar(value=cfg.morning_briefing_time)
    ttk.Entry(tab_auto, textvariable=morning_var, width=10).pack(anchor="w", pady=(0, 10))

    ttk.Label(tab_auto, text="Evening Wrap-Up Time (HH:MM):", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 2))
    evening_var = tk.StringVar(value=cfg.evening_wrapup_time)
    ttk.Entry(tab_auto, textvariable=evening_var, width=10).pack(anchor="w", pady=(0, 15))

    launch_login_var = tk.BooleanVar(value=cfg.launch_at_login)
    ttk.Checkbutton(tab_auto, text="Launch at Login (macOS LaunchAgent)", variable=launch_login_var).pack(anchor="w", pady=(0, 15))

    conflict_var = tk.BooleanVar(value=cfg.get("auto_conflict_resolution", True))
    ttk.Checkbutton(tab_auto, text="Natural Language Conflict Detection for Calendar", variable=conflict_var).pack(anchor="w", pady=(0, 10))

    tab_mail = ttk.Frame(notebook, padding=15)
    notebook.add(tab_mail, text="Mail")
    ttk.Label(tab_mail, text="Email provider:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 4))
    provider_var = tk.StringVar(value=cfg.get("mail_provider", "gmail"))
    provider_combo = ttk.Combobox(tab_mail, textvariable=provider_var, values=["gmail", "icloud"], state="readonly")
    provider_combo.pack(fill="x", pady=(0, 12))
    ttk.Label(tab_mail, text="Email address:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 4))
    mail_email_var = tk.StringVar(value=cfg.get("mail_email", ""))
    ttk.Entry(tab_mail, textvariable=mail_email_var).pack(fill="x", pady=(0, 12))
    ttk.Label(tab_mail, text="App password:", font=("Helvetica", 12, "bold")).pack(anchor="w", pady=(0, 4))
    mail_password_var = tk.StringVar()
    ttk.Entry(tab_mail, textvariable=mail_password_var, show="•").pack(fill="x", pady=(0, 6))
    ttk.Label(tab_mail, text="Stored in macOS Keychain. Leave blank to keep the saved password.",
              wraplength=450).pack(anchor="w", pady=(0, 15))
    instructions = tk.StringVar()
    ttk.Label(tab_mail, textvariable=instructions, wraplength=500).pack(anchor="w", pady=(0, 8))

    def show_mail_help(*_):
        if provider_var.get() == "gmail":
            instructions.set("Enable Google 2-Step Verification, then create an app password at myaccount.google.com/apppasswords. Use your full Gmail address. No OAuth setup is needed.")
        else:
            instructions.set("Create an app-specific password at account.apple.com → Sign-In and Security → App-Specific Passwords. Use your iCloud Mail address.")

    show_mail_help()

    def change_provider(_):
        mail_password_var.set("")
        mail_status.set("")
        show_mail_help()

    provider_combo.bind("<<ComboboxSelected>>", change_provider)

    def open_password_page():
        import webbrowser
        webbrowser.open("https://myaccount.google.com/apppasswords" if provider_var.get() == "gmail" else "https://account.apple.com/")

    ttk.Button(tab_mail, text="Create App Password…", command=open_password_page).pack(anchor="w", pady=(0, 12))
    mail_status = tk.StringVar()
    ttk.Label(tab_mail, textvariable=mail_status, wraplength=500).pack(anchor="w", pady=(0, 10))
    mail_results = queue.Queue()

    def poll_mail_connection():
        try:
            tested_provider, tested_account, error = mail_results.get_nowait()
        except queue.Empty:
            pass
        else:
            test_button.config(state="normal")
            if (tested_provider, tested_account) == (provider_var.get(), mail_email_var.get().strip().lower()):
                mail_status.set(error or "Connected. Save Preferences to use this account.")
        root.after(100, poll_mail_connection)

    root.after(100, poll_mail_connection)

    def check_mail():
        provider = provider_var.get()
        try:
            account = validate_account(mail_email_var.get(), provider)
        except ValueError as error:
            messagebox.showerror("Mail", str(error))
            return
        password = mail_password_var.get()
        test_button.config(state="disabled")
        mail_status.set("Checking connection…")

        def worker():
            try:
                secret = password if password.strip() else get_password(account, provider)
                if not secret:
                    raise ValueError("Enter an app password first.")
                test_connection(account, secret, provider)
                error = ""
            except (RuntimeError, OSError, ValueError) as failure:
                error = str(failure)
            mail_results.put((provider, account, error))

        threading.Thread(target=worker, daemon=True).start()

    def forget_mail():
        try:
            account = mail_email_var.get().strip().lower()
            if account:
                delete_password(account, provider_var.get())
            mail_password_var.set("")
            mail_status.set("Saved password removed from Keychain.")
        except RuntimeError as error:
            messagebox.showerror("Mail", str(error))

    test_button = ttk.Button(tab_mail, text="Test Connection", command=check_mail)
    test_button.pack(anchor="w", pady=(0, 10))
    ttk.Button(tab_mail, text="Remove Saved Password", command=forget_mail).pack(anchor="w", pady=(0, 15))
    ttk.Label(tab_mail, text="Calendar and Reminders use your Mac's synced accounts. Allow full access when macOS asks. Mail, Calendar, and Reminders stay closed during background work.",
              wraplength=500).pack(anchor="w")

    # --- BUTTON BAR ---
    btn_frame = ttk.Frame(root, padding=10)
    btn_frame.pack(fill="x", side="bottom")

    def save_and_close():
        try:
            interval = int(poll_var.get())
            if not 5 <= interval <= 120:
                raise ValueError("Inbox interval must be 5–120 minutes.")
            for value in (morning_var.get().strip(), evening_var.get().strip()):
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    raise ValueError("Briefing times must use HH:MM, such as 08:00.")
        except (ValueError, tk.TclError) as error:
            messagebox.showerror("Check Preferences", str(error))
            return
        provider = provider_var.get()
        account = mail_email_var.get().strip().lower()
        try:
            if account:
                validate_account(account, provider)
            if mail_password_var.get().strip():
                save_password(account, mail_password_var.get(), provider)
        except (RuntimeError, ValueError) as error:
            messagebox.showerror("Mail", str(error))
            return
        cfg.config["mail_provider"] = provider
        cfg.config["mail_email"] = account
        cfg.config.pop("icloud_email", None)
        cfg.config["openai_api_key"] = api_key_var.get().strip()
        cfg.config["openai_base_url"] = base_url_var.get().strip()
        cfg.config["text_model"] = text_model_var.get().strip()
        cfg.config["stt_model"] = stt_model_var.get().strip()
        cfg.config["ptt_hotkey"] = hotkey_var.get().strip()
        cfg.config["tts_enabled"] = tts_enabled_var.get()
        cfg.config["action_notifications"] = action_notifications_var.get()
        cfg.config["tts_voice"] = voice_var.get().strip()
        cfg.config["tts_speed"] = round(speed_var.get(), 2)
        cfg.config["poll_interval_minutes"] = interval
        cfg.config["morning_briefing_time"] = morning_var.get().strip()
        cfg.config["evening_wrapup_time"] = evening_var.get().strip()
        cfg.config["auto_conflict_resolution"] = conflict_var.get()

        # Handle launch at login
        if cfg.launch_at_login != launch_login_var.get():
            cfg.update_launch_at_login(launch_login_var.get())
        else:
            cfg.save()

        messagebox.showinfo("Saved", "Preferences saved successfully!")
        root.destroy()

    ttk.Button(btn_frame, text="Cancel", command=root.destroy).pack(side="right", padx=5)
    ttk.Button(btn_frame, text="Save Preferences", command=save_and_close).pack(side="right", padx=5)

    root.mainloop()


def open_settings_window():
    """Launch settings GUI non-blockingly via subprocess."""
    arguments = [sys.executable, "--settings"] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__), "--gui"]
    subprocess.Popen(arguments)


if __name__ == "__main__":
    if "--gui" in sys.argv:
        launch_settings_gui()
    else:
        cfg = AppConfig()
        print("MacAssistant Config Loaded:")
        print(f"  API Key: {'Set (length=' + str(len(cfg.openai_api_key)) + ')' if cfg.openai_api_key else 'Not set'}")
        print(f"  Base URL: {cfg.openai_base_url}")
        print(f"  Text Model: {cfg.text_model}")
        print(f"  STT Model: {cfg.stt_model}")
        print(f"  PTT Hotkey: {cfg.ptt_hotkey}")
        print(f"  Poll Interval: {cfg.poll_interval_minutes}m")
        print(f"  Launch at Login: {cfg.launch_at_login}")
