import unittest

import updater


class InstallerUpdateTests(unittest.TestCase):
    def test_create_batch_uses_silent_uninstall_and_installer(self):
        batch = updater.create_installer_update_batch(
            r"C:\Program Files\Piano Player\PianoPlayer.exe",
            r"C:\Users\me\AppData\Local\Temp\piano_player_update_123.exe",
            r"C:\Program Files\Piano Player\unins000.exe",
        )

        self.assertIn(r'if exist "C:\Program Files\Piano Player\unins000.exe"', batch)
        self.assertIn('/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', batch)
        self.assertIn('start /wait "" "C:\\Users\\me\\AppData\\Local\\Temp\\piano_player_update_123.exe"', batch)


if __name__ == "__main__":
    unittest.main()
