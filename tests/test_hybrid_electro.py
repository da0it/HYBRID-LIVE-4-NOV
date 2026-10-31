import copy
import importlib
import json
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from HybridElectro.core import (
    ConfigurationError, TargetError, load_config, resolve_target,
    resolve_track, validate_config, track_targets, resolve_session_track,
)
from tools.install import install
from tools.midi_log import summary
from tools.prepare_set import clean_midi_mappings
from HybridElectro.volume import UnityVolumeBinding


ROOT = Path(__file__).resolve().parents[1]


def config():
    result = load_config(ROOT / "HybridElectro" / "config.json")
    result.pop("track_macros", None)
    result.pop("pad_follow_session", None)
    result.pop("volume_follow_session", None)
    result["pads"] = [{"channel": 1, "note": 64, "track": "DECK A"},
                      {"channel": 1, "note": 65, "track": "BREAKS"}]
    result["tracks"] = {"DECK A": [{"mixer": "volume"}] + [None] * 7,
                        "BREAKS": [{"mixer": "volume"}] + [None] * 7}
    result["setup_mode"] = True
    return result


class Parameter:
    def __init__(self, name, original_name=None):
        self.name = name
        self.original_name = original_name or name
        self._value = 0.5

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        raise AssertionError("Track selection or parameter binding wrote a value")


class VolumeParameter(Parameter):
    min, max, default_value, is_enabled = 0.0, 1.0, 0.85, True

    def __init__(self, value=0.5):
        super().__init__("Volume")
        self._value = value
        self.writes = []

    def str_for_value(self, value):
        return "0.0 dB" if value == self.default_value else "+6.0 dB"

    @Parameter.value.setter
    def value(self, value):
        self.writes.append(value)
        self._value = value


class VolumeTests(unittest.TestCase):
    def test_full_midi_sweep_never_exceeds_zero_db_and_reaches_both_endpoints(self):
        p = VolumeParameter()
        binding = UnityVolumeBinding()
        binding.bind(p)
        self.assertEqual(p.writes, [])
        for value in range(128):
            binding.receive(value)
        self.assertEqual(p.value, p.default_value)
        for value in reversed(range(128)):
            binding.receive(value)
        self.assertEqual(p.value, p.min)
        self.assertTrue(all(p.min <= value <= p.default_value for value in p.writes))

    def test_pickup_on_rebind_and_mouse_edits_preserves_existing_values(self):
        a, b = VolumeParameter(0.425), VolumeParameter(0.7)
        binding = UnityVolumeBinding()
        binding.bind(a)
        binding.receive(10)
        binding.receive(60)
        self.assertEqual(a.writes, [])
        binding.receive(64)
        self.assertTrue(a.writes)
        binding.bind(b)
        binding.receive(64)
        self.assertEqual(b.writes, [])
        binding.receive(110)
        self.assertTrue(b.writes)
        b._value = 0.2  # independent mouse/automation edit
        count = len(b.writes)
        binding.receive(111)
        self.assertEqual(len(b.writes), count)
        binding.bind(None)
        binding.receive(127)
        self.assertEqual(len(b.writes), count)

    def test_binding_does_not_clamp_existing_boost_but_input_can_reach_zero(self):
        p = VolumeParameter(1.0)
        binding = UnityVolumeBinding()
        binding.bind(p)
        self.assertEqual(p.value, 1.0)
        self.assertEqual(p.writes, [])
        binding.receive(127)
        self.assertEqual(p.value, 0.85)
        p.is_enabled = False
        binding.receive(0)
        self.assertEqual(p.value, 0.85)

    def test_unverified_endpoint_fails_closed(self):
        binding = UnityVolumeBinding()
        p = VolumeParameter()
        p.str_for_value = lambda value: "-6.0 dB"
        with self.assertRaises(TargetError):
            binding.bind(p)
        binding.receive(127)
        self.assertEqual(p.writes, [])


def device(name, parameters=(), chains=()):
    return types.SimpleNamespace(name=name, parameters=list(parameters), chains=list(chains))


def track(name, devices=()):
    return types.SimpleNamespace(
        name=name, devices=list(devices),
        mixer_device=types.SimpleNamespace(volume=Parameter("Volume"), panning=Parameter("Pan"),
                                            sends=[Parameter("A"), Parameter("B")]))


def rack(name="PERFORM"):
    result = device(name, [Parameter("Device On")] +
                    [Parameter("Effect {}".format(i), "Macro {}".format(i)) for i in range(1, 17)])
    result.can_have_chains = True
    return result


def song():
    deck = track("DECK A", [device("Auto Filter", [Parameter("Frequency")])])
    breaks = track("BREAKS", [device("Beat Repeat", [Parameter("Grid")])])
    master = track("Master")
    master.mixer_device.crossfader = Parameter("Crossfader")
    return types.SimpleNamespace(tracks=[deck, breaks], return_tracks=[track("A Delay")],
                                 master_track=master, view=types.SimpleNamespace(selected_track=deck))


class CoreTests(unittest.TestCase):
    def test_capped_volume_configuration_rejects_wrong_limit_and_relative_mode(self):
        for limit in (-6, 6, True, "0", float("nan")):
            c = config()
            c["volume_follow_session"] = {"surface": "Launchpad", "max_db": limit}
            with self.assertRaises(ConfigurationError):
                validate_config(c)
        c = config()
        c["volume_follow_session"] = {"surface": "Launchpad", "max_db": 0}
        c["encoders"]["global"]["mode"] = "relative_two_complement"
        with self.assertRaises(ConfigurationError):
            validate_config(c)

    def test_volume_follow_configuration_rejects_invalid_source_or_missing_bank(self):
        for value in ({}, {"surface": ""}, {"surface": "Launchpad", "extra": True}):
            c = config()
            c["volume_follow_session"] = value
            with self.assertRaises(ConfigurationError):
                validate_config(c)
        c = config()
        c["volume_follow_session"] = {"surface": "Launchpad"}
        c["encoders"]["global"] = None
        with self.assertRaises(ConfigurationError):
            validate_config(c)
    def test_session_pads_follow_offset_and_visible_track_order(self):
        tracks = [track("Track {}".format(i)) for i in range(15)]
        ring = types.SimpleNamespace(width=lambda:8, track_offset=lambda:0,
                                     tracks_to_use=lambda:tracks)
        launchpad = type("Launchpad", (), {})()
        launchpad._highlighting_session_component = ring
        self.assertIs(resolve_session_track([launchpad], "Launchpad", 1), tracks[0])
        ring.track_offset = lambda:1
        self.assertIs(resolve_session_track([launchpad], "Launchpad", 1), tracks[1])
        self.assertIs(resolve_session_track([launchpad], "Launchpad", 8), tracks[8])
        ring.track_offset = lambda:8
        self.assertIs(resolve_session_track([launchpad], "Launchpad", 1), tracks[8])
        with self.assertRaises(TargetError):
            resolve_session_track([launchpad], "Launchpad", 8)
        del tracks[0]  # folding/removing tracks changes the controller's visible list
        self.assertIs(resolve_session_track([launchpad], "Launchpad", 1), tracks[8])

    def test_missing_or_ambiguous_launchpad_fails_closed(self):
        with self.assertRaises(TargetError):
            resolve_session_track([], "Launchpad", 1)
        a, b = type("Launchpad", (), {})(), type("Launchpad", (), {})()
        with self.assertRaises(TargetError):
            resolve_session_track([a,b], "Launchpad", 1)
        a._highlighting_session_component = None
        with self.assertRaises(TargetError):
            resolve_session_track([a], "Launchpad", 1)

    def test_legacy_configuration_without_macros_is_supported(self):
        legacy = config()
        self.assertNotIn("track_macros", legacy)
        self.assertEqual(track_targets(legacy, track("UNKNOWN")), [None] * 8)
        validate_config(legacy)

    def test_macro_names_survive_user_renaming(self):
        s = song()
        r = rack()
        s.tracks[0].devices.append(r)
        self.assertIs(resolve_target(s, s.tracks[0], {"rack": "PERFORM", "macro": 3}),
                      r.parameters[3])
        r.parameters[3].name = "My delay feedback"
        self.assertIs(resolve_target(s, s.tracks[0], {"rack": "PERFORM", "macro": 3}),
                      r.parameters[3])

    def test_perform_must_be_a_unique_rack(self):
        s = song()
        target = {"rack": "PERFORM", "macro": 1}
        s.tracks[0].devices = [device("PERFORM", [Parameter("Macro 1")])]
        with self.assertRaisesRegex(TargetError, "not a Rack"):
            resolve_target(s, s.tracks[0], target)
        s.tracks[0].devices = [rack(), rack()]
        with self.assertRaisesRegex(TargetError, "Ambiguous"):
            resolve_target(s, s.tracks[0], target)

    def test_global_filter_ignores_another_filter_inside_perform(self):
        s = song()
        t = s.tracks[0]
        expected = t.devices[0].parameters[0]
        chain = types.SimpleNamespace(name="FX", devices=[device("Auto Filter", [Parameter("Frequency")])])
        t.devices.append(device("PERFORM", chains=[chain]))
        target = {"device": "Auto Filter", "parameter": "Frequency", "device_scope": "top_level"}
        self.assertIs(resolve_target(s, t, target), expected)

    def test_macro_indices_out_of_range_fail_validation(self):
        for number in (0, 17, True):
            c = config()
            c["tracks"]["DECK A"][0] = {"rack": "PERFORM", "macro": number}
            with self.assertRaises(ConfigurationError):
                validate_config(c)

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

    def add_value_listener(self, listener):
        self.listener = listener

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
    def test_capped_volume_input_resolves_ring_before_writing_and_never_native_maps(self):
        self.config["encoders"]["global"]["mode"] = "absolute"
        self.config["volume_follow_session"] = {"surface": "Launchpad", "max_db": 0}
        for t in self.song.tracks:
            t.mixer_device.volume = VolumeParameter(0.85)
        offset = [0]
        launchpad = type("Launchpad", (), {})()
        launchpad._highlighting_session_component = types.SimpleNamespace(
            width=lambda: 8, track_offset=lambda: offset[0], tracks_to_use=lambda: self.song.tracks)
        with patch.object(self.surface, "get_control_surfaces", return_value=[launchpad]):
            surface = self.make_surface()
            knobs = surface._knob_banks["global"]
            self.assertTrue(all(k.parameter is None for k in knobs))
            first, second = (t.mixer_device.volume for t in self.song.tracks)
            self.assertEqual(first.writes, [])
            self.assertEqual(second.writes, [])
            offset[0] = 1  # input arrives before the next display tick
            knobs[0].listener(127)
            self.assertEqual(first.writes, [])
            self.assertEqual(second.writes, [0.85])
            knobs[0].listener(126)
            self.assertLess(second.value, 0.85)
            offset[0] = 2  # no tracks left: release the old target
            knobs[0].listener(127)
            self.assertEqual(len(second.writes), 2)

    def test_volumes_follow_ring_without_selection_or_value_changes(self):
        self.config["volume_follow_session"] = {"surface": "Launchpad"}
        self.song.tracks.extend(track("Extra {}".format(i)) for i in range(10))
        tracks = self.song.tracks
        launchpad = type("Launchpad", (), {})()
        offset = [0]
        visible = list(tracks)
        launchpad._highlighting_session_component = types.SimpleNamespace(
            width=lambda: 8, track_offset=lambda: offset[0], tracks_to_use=lambda: visible)
        values = [t.mixer_device.volume.value for t in tracks]
        with patch.object(self.surface, "get_control_surfaces", return_value=[launchpad]):
            surface = self.make_surface()
            knobs = surface._knob_banks["global"]
            track_knob = surface._knob_banks["track"][0].parameter
            self.assertEqual([k.parameter for k in knobs], [t.mixer_device.volume for t in visible[:8]])
            offset[0] = 1
            surface.update_display()
            self.assertEqual([k.parameter for k in knobs], [t.mixer_device.volume for t in visible[1:9]])
            del visible[2]  # folded/removed track follows Launchpad's actual list
            surface.update_display()
            self.assertEqual([k.parameter for k in knobs], [t.mixer_device.volume for t in visible[1:9]])
            offset[0] = 8
            surface.update_display()
            self.assertEqual([k.parameter for k in knobs],
                             [t.mixer_device.volume for t in visible[8:]] + [None] * 5)
            self.assertIs(surface._knob_banks["track"][0].parameter, track_knob)
            self.assertIs(self.song.view.selected_track, tracks[0])
            before = [k.connections for k in knobs]
            surface.update_display()
            self.assertEqual([k.connections for k in knobs], before)
        with patch.object(self.surface, "get_control_surfaces", return_value=[]):
            surface.update_display()
            self.assertTrue(all(k.parameter is None for k in knobs))
        self.assertEqual([t.mixer_device.volume.value for t in tracks], values)

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
            "_Framework.ControlSurface": types.SimpleNamespace(
                ControlSurface=FakeControlSurface, get_control_surfaces=lambda:[]),
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
        self.config["encoders"]["global"]["mode"] = "relative_two_complement"
        self.config["tracks"] = {
            "DECK A": [{"mixer": "volume"},
                       {"device": "Auto Filter", "parameter": "Frequency"}] + [None] * 6,
            "BREAKS": [{"device": "Beat Repeat", "parameter": "Grid"},
                       {"mixer": "volume"}] + [None] * 6,
        }
        self.config["global"] = [{"track": "DECK A", "mixer": "volume"}] + [None] * 7
        self.config["pads"] = [{"channel": 1, "note": 64, "track": "DECK A"},
                               {"channel": 1, "note": 65, "track": "BREAKS"}]

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

    def test_macros_follow_more_than_eight_tracks_without_resetting_values(self):
        self.config["tracks"] = {}
        self.config["track_macros"] = {"rack_name": "PERFORM"}
        self.song.tracks.extend(track("Extra {}".format(i)) for i in range(10))
        parameters = []
        for index, t in enumerate(self.song.tracks):
            r = rack()
            t.devices.append(r)
            for i, p in enumerate(r.parameters[1:9]):
                p._value = (index * 8 + i) / 127.0  # existing, independently edited Live state
                parameters.append(p)
        before = [p.value for p in parameters]
        surface = self.make_surface()
        global_parameter = surface._knob_banks["global"][0].parameter
        for t in self.song.tracks + list(reversed(self.song.tracks)):
            self.song.view.selected_track = t
            surface._selection_changed()
            expected = t.devices[-1].parameters[1:9]
            self.assertEqual([k.parameter for k in surface._knob_banks["track"]], expected)
            self.assertTrue(all(k.takeover for k in surface._knob_banks["track"]))
            self.assertIs(surface._knob_banks["global"][0].parameter, global_parameter)
        self.assertEqual([p.value for p in parameters], before)

    def test_track_rename_and_new_rack_do_not_require_config_edits(self):
        self.config["tracks"] = {}
        self.config["track_macros"] = {"rack_name": "PERFORM"}
        surface = self.make_surface()
        t = self.song.tracks[0]
        t.name = "MY NEW NAME"
        r = rack()
        t.devices.append(r)
        surface._refresh()
        self.assertIs(surface._knob_banks["track"][0].parameter, r.parameters[1])
        t.devices.remove(r)
        surface._refresh()
        self.assertTrue(all(k.parameter is None for k in surface._knob_banks["track"]))

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

    def test_mapping_diagnostics_report_native_result_without_writing_values(self):
        self.config["dump_parameters"] = True
        surface = self.make_surface()
        knob = surface._knob_banks["track"][0]
        knob._is_mapped = False
        surface.build_midi_map(99)
        rows = [json.loads(line.split(" MAPPING ", 1)[1]) for line in surface.logs
                if " MAPPING " in line]
        row = next(row for row in rows if row["bank"] == "track" and row["knob"] == 1)
        self.assertFalse(row["mapped"])
        self.assertEqual(row["cc"], 38)
        self.assertEqual(row["name"], "Volume")
        self.assertEqual(row["value"], 0.5)
        self.assertEqual(self.forward_cc, [])

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

    def test_pad_resolves_new_ring_position_at_press_time(self):
        self.config["pad_follow_session"] = {"surface":"Launchpad"}
        self.config["pads"] = [{"channel":1,"note":64,"track_slot":1}]
        launchpad = type("Launchpad", (), {})()
        ring = types.SimpleNamespace(width=lambda:8,track_offset=lambda:0,
                                     tracks_to_use=lambda:self.song.tracks)
        launchpad._highlighting_session_component = ring
        pads = self.make_pads()
        with patch.object(self.pads, "get_control_surfaces", return_value=[launchpad]):
            ring.track_offset = lambda:1
            pads._buttons[0].listener(0)
            self.assertIs(self.song.view.selected_track,self.song.tracks[0])
            pads._buttons[0].listener(100)
            self.assertIs(self.song.view.selected_track,self.song.tracks[1])

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
    def test_set_cleanup_preserves_values_and_unrelated_assignments(self):
        root = ET.fromstring('''<LiveSet><Param><Manual Value="0.8"/>
            <KeyMidi><IsNote Value="false"/><Channel Value="0"/>
            <NoteOrController Value="38"/></KeyMidi></Param>
            <Other><Manual Value="0.3"/><KeyMidi><IsNote Value="false"/>
            <Channel Value="0"/><NoteOrController Value="5"/></KeyMidi></Other>
            <OtherChannel><KeyMidi><IsNote Value="false"/><Channel Value="1"/>
            <NoteOrController Value="38"/></KeyMidi></OtherChannel></LiveSet>''')
        values = [n.get("Value") for n in root.iter("Manual")]
        removed = clean_midi_mappings(root, config())
        self.assertEqual(len(removed), 1)
        self.assertIsNone(root.find("Param/KeyMidi"))
        self.assertIsNotNone(root.find("Other/KeyMidi"))
        self.assertIsNotNone(root.find("OtherChannel/KeyMidi"))
        self.assertEqual(values, [n.get("Value") for n in root.iter("Manual")])

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
