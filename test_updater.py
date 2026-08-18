import os
import tempfile
import unittest

import mido

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

    def test_midi_to_sheet_text_creates_parseable_sheet(self):
        midi = mido.MidiFile()
        track = mido.MidiTrack()
        midi.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=64, time=0))
        track.append(mido.Message("note_off", note=60, velocity=64, time=96))
        track.append(mido.Message("note_on", note=64, velocity=64, time=0))
        track.append(mido.Message("note_off", note=64, velocity=64, time=96))

        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as handle:
            midi.save(handle.name)
            sheet_text = main.midi_to_sheet_text(handle.name)

        try:
            self.assertTrue(sheet_text.strip())
            events = main.SheetParser.parse(sheet_text)
            self.assertTrue(events)
        finally:
            os.remove(handle.name)

    def test_apply_timing_scale_updates_selection_durations(self):
        note_event = {"type": "note", "actions": [("a", False)], "beats": 1.0}
        rest_event = {"type": "rest", "length": 0.5, "source": " "}

        main.apply_timing_scale(note_event, 1.5)
        main.apply_timing_scale(rest_event, 1.5)

        self.assertEqual(note_event["beats"], 1.5)
        self.assertEqual(rest_event["length"], 0.75)


if __name__ == "__main__":
    unittest.main()
