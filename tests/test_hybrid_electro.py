import copy
import importlib
import json
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from HybridElectro.core import (
    ConfigurationError, TargetError, load_config, resolve_target,
    resolve_track, validate_config,
)
from tools.install import install
from tools.midi_log import summary


ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(ROOT / "HybridElectro" / "config.json")


class Parameter:
    def __init__(self, name, original_name=None):
        self.name = name
        self.original_name = original_name or name

    @property
    def value(self):
        return 0.5

    @value.setter
    def value(self, value):
        raise AssertionError("Track selection or parameter binding wrote a value")


def device(name, parameters=(), chains=()):
    return types.SimpleNamespace(name=name, parameters=list(parameters), chains=list(chains))


def track(name, devices=()):
    return types.SimpleNamespace(
        name=name, devices=list(devices),
        mixer_device=types.SimpleNamespace(volume=Parameter("Volume"), panning=Parameter("Pan"),
                                            sends=[Parameter("A"), Parameter("B")]))


def song():
    deck = track("DECK A", [device("Auto Filter", [Parameter("Frequency")])])
    breaks = track("BREAKS", [device("Beat Repeat", [Parameter("Grid")])])
    master = track("Master")
    master.mixer_device.crossfader = Parameter("Crossfader")
    return types.SimpleNamespace(tracks=[deck, breaks], return_tracks=[track("A Delay")],
                                 master_track=master, view=types.SimpleNamespace(selected_track=deck))


class CoreTests(unittest.TestCase):
    def test_shipped_configuration_starts_in_setup_mode(self):
        self.assertTrue(config()["setup_mode"])

    def test_conflicting_banks_are_rejected_but_same_cc_on_different_channel_is_allowed(self):
        c = config()
        c["encoders"]["global"]["cc"] = c["encoders"]["track"]["cc"][:]
        with self.assertRaises(ConfigurationError):
            validate_config(c)
        c["encoders"]["global"]["channel"] = 2
        validate_config(c)

    def test_invalid_midi_and_modes_are_rejected(self):
        for field, value in (("cc", [1] * 8), ("cc", [128] + list(range(7))),
                             ("channel", 0), ("channel", True), ("mode", "guess"),
                             ("sensitivity", float("nan"))):
            with self.subTest(field=field, value=value):
                c = config()
                c["encoders"]["track"][field] = value
                with self.assertRaises(ConfigurationError):
                    validate_config(c)

    def test_bad_global_target_cannot_follow_selected_track_accidentally(self):
        c = config()
        c["global"][0] = {"mixer": "volume"}
        with self.assertRaises(ConfigurationError):
            validate_config(c)

    def test_ambiguous_target_schema_and_unknown_keys_fail(self):
        for target in ({"mixer": "volume", "send": 1}, {"mixer": "volume", "typo": 3},
                       {"send": 0}, {"track": "@return:0", "mixer": "volume"}):
            c = config()
            c["tracks"]["DECK A"][0] = target
            with self.assertRaises(ConfigurationError):
                validate_config(c)

    def test_repeated_pad_message_is_rejected(self):
        c = config()
        c["pads"].append(copy.deepcopy(c["pads"][0]))
        with self.assertRaises(ConfigurationError):
            validate_config(c)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"version": 1, "version": 1}')
            with self.assertRaisesRegex(ConfigurationError, "Duplicate JSON key"):
                load_config(path)

    def test_unknown_named_track_never_falls_back_to_selected(self):
        s = song()
        with self.assertRaises(TargetError):
            resolve_target(s, s.view.selected_track, {"track": "MISSING", "mixer": "volume"})

    def test_duplicate_track_names_fail(self):
        s = song()
        s.tracks.append(track("DECK A"))
        with self.assertRaisesRegex(TargetError, "Ambiguous"):
            resolve_track(s, "DECK A")

    def test_mixer_and_returns_and_one_based_sends(self):
        s = song()
        t = s.view.selected_track
        self.assertIs(resolve_target(s, t, {"mixer": "volume"}), t.mixer_device.volume)
        self.assertIs(resolve_target(s, t, {"send": 2}), t.mixer_device.sends[1])
        self.assertIs(resolve_track(s, "@master"), s.master_track)
        self.assertIs(resolve_track(s, "@return:1"), s.return_tracks[0])
        with self.assertRaises(TargetError):
            resolve_track(s, "@return:2")
        with self.assertRaises(TargetError):
            resolve_target(s, t, {"send": 3})

    def test_nested_device_and_original_parameter_name(self):
        s = song()
        parameter = Parameter("Частота", "Frequency")
        chain = types.SimpleNamespace(name="Chain", devices=[device("My Filter", [parameter])])
        s.tracks[0].devices = [device("Rack", chains=[chain])]
        self.assertIs(resolve_target(s, s.tracks[0],
                                    {"device": "My Filter", "parameter": "Frequency"}), parameter)

    def test_duplicate_devices_and_parameters_fail(self):
        s = song()
        t = s.tracks[0]
        t.devices.append(device("Auto Filter", [Parameter("Frequency")]))
        target = {"device": "Auto Filter", "parameter": "Frequency"}
        with self.assertRaisesRegex(TargetError, "Ambiguous"):
            resolve_target(s, t, target)
        t.devices.pop()
        t.devices[0].parameters.append(Parameter("Frequency"))
        with self.assertRaisesRegex(TargetError, "Ambiguous"):
            resolve_target(s, t, target)


class FakeControlSurface:
    def __init__(self, instance):
        self._c_instance = instance
        self.controls = []
        self.messages = []
        self.logs = []

    def component_guard(self):
        return nullcontext()

    def song(self):
        return self._c_instance.song

    def register_slot(self, subject, callback, event):
        pass

    def log_message(self, text):
        self.logs.append(text)

    def show_message(self, text):
        self.messages.append(text)

    def build_midi_map(self, handle):
        pass

    def receive_midi(self, message):
        pass

    def update_display(self):
        pass

    def disconnect(self):
        pass


class FakeEncoder:
    def __init__(self, msg_type, channel, cc, map_mode, name):
        self.channel, self.cc, self.mode, self.name = channel, cc, map_mode, name
        self.parameter = None
        self.connections = 0

    def set_needs_takeover(self, enabled):
        self.takeover = enabled

    def release_parameter(self):
        self.parameter = None

    def connect_to(self, parameter):
        self.parameter = parameter
        self.connections += 1


class FakeButton:
    def __init__(self, momentary, msg_type, channel, note, name):
        self.channel, self.note = channel, note

    def add_value_listener(self, listener):
        self.listener = listener


class SurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modes = {name: name for name in (
            "absolute", "relative_smooth_two_compliment", "relative_smooth_signed_bit",
            "relative_smooth_binary_offset")}
        cls.forward_cc = []
        cls.forward_notes = []
        midi = types.SimpleNamespace(MapMode=types.SimpleNamespace(**modes),
                                      forward_midi_cc=lambda *args: cls.forward_cc.append(args),
                                      forward_midi_note=lambda *args: cls.forward_notes.append(args))
        modules = {
            "Live": types.SimpleNamespace(MidiMap=midi),
            "_Framework": types.ModuleType("_Framework"),
            "_Framework.ControlSurface": types.SimpleNamespace(ControlSurface=FakeControlSurface),
            "_Framework.EncoderElement": types.SimpleNamespace(EncoderElement=FakeEncoder),
            "_Framework.ButtonElement": types.SimpleNamespace(ButtonElement=FakeButton),
            "_Framework.InputControlElement": types.SimpleNamespace(MIDI_CC_TYPE=1, MIDI_NOTE_TYPE=0),
        }
        cls.module_patch = patch.dict(sys.modules, modules)
        cls.module_patch.start()
        cls.surface = importlib.import_module("HybridElectro.surface")
        cls.pads = importlib.import_module("HybridElectro.pads")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()
        sys.modules.pop("HybridElectro.surface", None)
        sys.modules.pop("HybridElectro.pads", None)

    def setUp(self):
        self.forward_cc.clear()
        self.forward_notes.clear()
        self.song = song()
        self.instance = types.SimpleNamespace(song=self.song, handle=lambda: 42)
        self.config = config()
        self.config["setup_mode"] = False
        self.config["dump_parameters"] = False
        self.config["encoders"]["track"]["mode"] = "absolute"
        self.config["tracks"] = {
            "DECK A": [{"mixer": "volume"},
                       {"device": "Auto Filter", "parameter": "Frequency"}] + [None] * 6,
            "BREAKS": [{"device": "Beat Repeat", "parameter": "Grid"},
                       {"mixer": "volume"}] + [None] * 6,
        }
        self.config["global"] = [{"track": "DECK A", "mixer": "volume"}] + [None] * 7

    def make_surface(self):
        with patch.object(self.surface, "load_config", return_value=self.config):
            return self.surface.HybridElectro(self.instance)

    def make_pads(self):
        with patch.object(self.surface, "load_config", return_value=self.config):
            return self.pads.HybridElectroPads(self.instance)

    def test_selection_rebinds_without_value_writes_and_global_stays_fixed(self):
        surface = self.make_surface()
        knobs = surface._knob_banks["track"]
        global_knob = surface._knob_banks["global"][0]
        self.assertIs(knobs[0].parameter, self.song.tracks[0].mixer_device.volume)
        self.assertTrue(knobs[0].takeover)
        self.assertEqual(knobs[0].channel, 0)
        self.assertFalse(global_knob.takeover)
        self.song.view.selected_track = self.song.tracks[1]
        surface._selection_changed()
        self.assertIs(knobs[0].parameter, self.song.tracks[1].devices[0].parameters[0])
        self.assertIs(global_knob.parameter, self.song.tracks[0].mixer_device.volume)
        before = [k.connections for k in knobs]
        surface._refresh()
        self.assertEqual(before, [k.connections for k in knobs])

    def test_unconfigured_track_clears_previous_targets(self):
        surface = self.make_surface()
        self.song.view.selected_track = self.song.master_track
        surface._selection_changed()
        self.assertTrue(all(k.parameter is None for k in surface._knob_banks["track"]))
        self.assertIsNotNone(surface._knob_banks["global"][0].parameter)

    def test_deleted_device_unmaps_and_new_device_recovers(self):
        surface = self.make_surface()
        knob = surface._knob_banks["track"][1]
        self.song.tracks[0].devices.clear()
        for _ in range(20):
            surface.update_display()
        self.assertIsNone(knob.parameter)
        self.assertEqual(len(surface.logs), 2)  # ready + one missing-device diagnostic
        replacement = Parameter("Frequency")
        self.song.tracks[0].devices.append(device("Auto Filter", [replacement]))
        surface._refresh()
        self.assertIs(knob.parameter, replacement)

    def test_setup_creates_no_bindings_and_forwards_only_own_port_cc(self):
        self.config["setup_mode"] = True
        surface = self.make_surface()
        self.assertEqual(surface._knob_banks, {})
        surface.build_midi_map(99)
        self.assertEqual(len(self.forward_cc), 16 * 128)
        self.assertEqual(self.forward_cc[0], (42, 99, 0, 0))
        self.assertEqual(self.forward_notes, [])
        surface.receive_midi((0xB9, 23, 127))
        self.assertIn("channel=10 cc=23 value=127", surface.logs[-1])

    def test_invalid_configuration_fails_closed(self):
        with patch.object(self.surface, "load_config", side_effect=ConfigurationError("bad CC")):
            surface = self.surface.HybridElectro(self.instance)
        for _ in range(20):
            surface.update_display()
        surface.build_midi_map(99)
        self.assertEqual(surface._knob_banks, {})
        self.assertEqual(self.forward_cc, [])
        self.assertIn("CONFIG ERROR", surface.logs[0])

    def test_pad_selects_only_on_note_on_and_missing_target_keeps_selection(self):
        pads = self.make_pads()
        button = pads._buttons[1]
        button.listener(0)
        self.assertIs(self.song.view.selected_track, self.song.tracks[0])
        button.listener(100)
        self.assertIs(self.song.view.selected_track, self.song.tracks[1])
        pads._select("MISSING", 100)
        self.assertIs(self.song.view.selected_track, self.song.tracks[1])

    def test_pad_setup_logs_notes_without_selecting(self):
        self.config["setup_mode"] = True
        pads = self.make_pads()
        pads.build_midi_map(11)
        self.assertEqual(len(self.forward_notes), 16 * 128)
        self.assertEqual(self.forward_cc, [])
        pads.receive_midi((0x99, 41, 100))
        self.assertIn("channel=10 note=41 value=100 on", pads.logs[-1])
        pads.receive_midi((0x99, 41, 0))
        self.assertTrue(pads.logs[-1].endswith("off"))
        self.assertIs(self.song.view.selected_track, self.song.tracks[0])


class ToolsTests(unittest.TestCase):
    def test_installer_backs_up_and_preserves_user_config(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("builtins.print"):
                destination = install(Path(directory))
                user_config = config()
                user_config["tracks"]["MY TRACK"] = [None] * 8
                installed_config = destination / "HybridElectro" / "config.json"
                installed_config.write_text(json.dumps(user_config))
                install(Path(directory))
                self.assertIn("MY TRACK", load_config(installed_config)["tracks"])
                backups = list((Path(directory) / "HybridElectro Backups").glob("*/HybridElectro/config.json"))
                self.assertEqual(len(backups), 1)
                self.assertIn("MY TRACK", load_config(backups[0])["tracks"])
                install(Path(directory), replace_config=True)
                self.assertNotIn("MY TRACK", load_config(installed_config)["tracks"])

    def test_log_summary_distinguishes_channels_and_ignores_note_off(self):
        lines = ["HybridElectro MIDI CC channel=1 cc=16 value=1",
                 "HybridElectro MIDI CC channel=2 cc=16 value=127",
                 "HybridElectroPads MIDI NOTE channel=10 note=40 value=100 on",
                 "HybridElectroPads MIDI NOTE channel=10 note=40 value=0 off"]
        with patch("builtins.print"):
            values = summary(lines)
        self.assertEqual(values[("CC", 1, 16)], [1])
        self.assertEqual(values[("CC", 2, 16)], [127])
        self.assertEqual(values[("NOTE", 10, 40)], [100])


if __name__ == "__main__":
    unittest.main()
