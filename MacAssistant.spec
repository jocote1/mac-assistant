# Rebuild: .venv/bin/python -m PyInstaller --noconfirm MacAssistant.spec
from PyInstaller.utils.hooks import collect_all, copy_metadata
from pathlib import Path

root = Path(SPECPATH)
datas, binaries, hiddenimports = [], [], [
    'pynput.keyboard._darwin', 'pynput.mouse._darwin', 'HIServices',
    'tkinter', 'tkinter.ttk', 'tkinter.messagebox',
]
for package in ('kokoro_onnx', 'espeakng_loader', 'phonemizer', 'onnxruntime', 'language_tags', 'segments', 'csvw'):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
datas += copy_metadata('kokoro-onnx')
datas += [(str(root / 'assets'), 'assets')]
a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, excludes=['pytest', 'IPython'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='MacAssistant',
          debug=False, strip=False, upx=False, console=False, target_arch='arm64',
          codesign_identity='-', entitlements_file=str(root / 'packaging' / 'entitlements.plist'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MacAssistant')
app = BUNDLE(coll, name='MacAssistant.app', bundle_identifier='no.olegrimstad.macassistant',
             icon=str(root / 'assets' / 'AppIcon.icns'),
             info_plist={
                 'CFBundleDisplayName': 'MacAssistant',
                 'CFBundleShortVersionString': '1.2.1',
                 'CFBundleVersion': '5',
                 'LSUIElement': True,
                 'NSHighResolutionCapable': True,
                 'NSMicrophoneUsageDescription': 'MacAssistant records your voice while you hold the push-to-talk shortcut.',
                 'NSAppleEventsUsageDescription': 'MacAssistant opens Apple Mail only when you ask to see an email.',
                 'NSCalendarsFullAccessUsageDescription': 'MacAssistant reads and manages your calendar in the background when you ask.',
                 'NSRemindersFullAccessUsageDescription': 'MacAssistant reads and manages your reminders in the background when you ask.',
                 'NSCalendarsUsageDescription': 'MacAssistant reads and manages your calendar in the background when you ask.',
                 'NSRemindersUsageDescription': 'MacAssistant reads and manages your reminders in the background when you ask.',
             })
