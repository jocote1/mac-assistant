"""Native macOS global hotkeys, with press and release events for push-to-talk."""
import ctypes
import sys


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


EventHandler = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class GlobalHotkey:
    """Register a shortcut without monitoring arbitrary keyboard input."""
    SHORTCUTS = {
        "cmd+5": (23, 1 << 8), "cmd5": (23, 1 << 8), "command+5": (23, 1 << 8),
        "ctrl+space": (49, 1 << 12), "f6": (97, 0), "space": (49, 0),
    }

    def __init__(self, shortcut, on_press, on_release):
        if shortcut.lower().strip() not in self.SHORTCUTS:
            raise ValueError("This key requires the modifier-key listener")
        self.keycode, self.modifiers = self.SHORTCUTS[shortcut.lower().strip()]
        self.on_press, self.on_release = on_press, on_release
        self._hotkey = ctypes.c_void_p()
        self._handler = ctypes.c_void_p()
        self._callback = EventHandler(self._event)
        self.carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        self.carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        self.carbon.GetEventKind.argtypes = [ctypes.c_void_p]
        self.carbon.GetEventKind.restype = ctypes.c_uint32
        self.carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, EventHandler, ctypes.c_uint32,
            ctypes.POINTER(EventTypeSpec), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.carbon.InstallEventHandler.restype = ctypes.c_int32
        self.carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        self.carbon.RegisterEventHotKey.restype = ctypes.c_int32
        self.carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]

    def start(self):
        target = self.carbon.GetApplicationEventTarget()
        keyb = int.from_bytes(b"keyb", "big")
        types = (EventTypeSpec * 2)(EventTypeSpec(keyb, 5), EventTypeSpec(keyb, 6))
        status = self.carbon.InstallEventHandler(target, self._callback, 2, types, None, ctypes.byref(self._handler))
        if status:
            raise RuntimeError(f"Could not install hotkey handler (macOS {status})")
        status = self.carbon.RegisterEventHotKey(self.keycode, self.modifiers,
            EventHotKeyID(int.from_bytes(b"MAss", "big"), 1), target, 0, ctypes.byref(self._hotkey))
        if status:
            self.stop()
            raise RuntimeError(f"Could not register shortcut (macOS {status}); another app may be using it")

    def _event(self, next_handler, event, context):
        try:
            kind = self.carbon.GetEventKind(event)
            if kind == 5:
                self.on_press()
            elif kind == 6:
                self.on_release()
        except Exception as error:
            print(f"[PTT] Hotkey callback failed: {error}", file=sys.stderr)
        return 0

    def stop(self):
        if self._hotkey.value:
            self.carbon.UnregisterEventHotKey(self._hotkey)
            self._hotkey = ctypes.c_void_p()
        if self._handler.value:
            self.carbon.RemoveEventHandler(self._handler)
            self._handler = ctypes.c_void_p()
