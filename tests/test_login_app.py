"""Validate login registration for the packaged app without touching login services."""
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import config


class LoginAppTests(unittest.TestCase):
    def test_packaged_login_uses_launchservices_and_preserves_app_path(self):
        cfg = config.AppConfig.__new__(config.AppConfig)
        cfg.config = {}
        cfg.save = Mock()
        app = Path('/Users/Example/Applications/MacAssistant.app')
        with tempfile.TemporaryDirectory() as folder:
            plist = Path(folder) / 'com.user.macassistant.plist'
            with patch.object(config.sys, 'frozen', True, create=True), \
                 patch.object(config.sys, 'executable', str(app / 'Contents/MacOS/MacAssistant')), \
                 patch.object(config, 'LAUNCH_AGENTS_DIR', Path(folder)), \
                 patch.object(config, 'PLIST_FILE', plist), \
                 patch.object(Path, 'is_dir', return_value=True), \
                 patch.object(config.subprocess, 'run', return_value=Mock(returncode=0)) as run:
                self.assertTrue(cfg.update_launch_at_login(True))
            data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data['ProgramArguments'], ['/usr/bin/open', '-g', str(app)])
        self.assertTrue(data['RunAtLoad'])
        self.assertFalse(data['KeepAlive'])
        self.assertTrue(cfg.config['launch_at_login'])
        self.assertEqual(run.call_args_list[-1].args[0][1], 'bootstrap')


if __name__ == '__main__':
    unittest.main()
