import unittest

import main
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

    def test_parser_supports_parallel_chunked_parsing(self):
        sheet = "a b [cde] {fgh} -\nq w e\n"
        events = main.SheetParser.parse(sheet, max_workers=2)
        self.assertEqual(len(events), 14)
        self.assertEqual([event["type"] for event in events], [
            "note", "rest", "note", "rest", "chord", "rest", "run", "rest",
            "rest", "note", "rest", "note", "rest", "note"
        ])


if __name__ == "__main__":
    unittest.main()
