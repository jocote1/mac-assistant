"""Native shortcut dispatch tests without recording or keyboard monitoring."""
import unittest
from unittest.mock import Mock, patch

from global_hotkey import GlobalHotkey


class GlobalHotkeyTests(unittest.TestCase):
    def test_cmd5_uses_native_keycode_and_command_modifier(self):
        carbon = Mock()
        with patch('global_hotkey.ctypes.CDLL', return_value=carbon):
            hotkey = GlobalHotkey('cmd+5', Mock(), Mock())
        self.assertEqual((hotkey.keycode, hotkey.modifiers), (23, 256))

    def test_native_press_and_release_dispatch_to_recording_callbacks(self):
        carbon = Mock()
        down, up = Mock(), Mock()
        with patch('global_hotkey.ctypes.CDLL', return_value=carbon):
            hotkey = GlobalHotkey('cmd+5', down, up)
        carbon.GetEventKind.return_value = 5
        self.assertEqual(hotkey._event(None, None, None), 0)
        down.assert_called_once()
        up.assert_not_called()
        carbon.GetEventKind.return_value = 6
        hotkey._event(None, None, None)
        up.assert_called_once()

    def test_registration_failure_is_reported(self):
        carbon = Mock()
        carbon.InstallEventHandler.return_value = 0
        carbon.RegisterEventHotKey.return_value = -9878
        with patch('global_hotkey.ctypes.CDLL', return_value=carbon):
            hotkey = GlobalHotkey('cmd+5', Mock(), Mock())
            with self.assertRaisesRegex(RuntimeError, 'register shortcut'):
                hotkey.start()


if __name__ == '__main__':
    unittest.main()
