"""Live adapter: native parameter mapping and a read-only MIDI setup mode."""

import os
import json
from functools import partial

import Live
from _Framework.ControlSurface import ControlSurface, get_control_surfaces
from _Framework.EncoderElement import EncoderElement
from _Framework.InputControlElement import MIDI_CC_TYPE

from .core import (MAP_MODES, TargetError, describe_track, load_config, resolve_target,
                   resolve_session_track, track_targets)
from .volume import UnityVolumeBinding


CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


class SetupSurface(ControlSurface):
    """Shared by the two independent MIDI input ports."""

    label = "HybridElectro"
    probe_cc = True
    probe_notes = False

    def __init__(self, c_instance):
        super().__init__(c_instance)
        self.config = None
        self._diagnostics = set()
        try:
            self.config = load_config(CONFIG_PATH)
        except (OSError, ValueError, TypeError) as error:
            self.log_message(self.label + " CONFIG ERROR: " + str(error))
            self.show_message(self.label + ": config.json error; see Log.txt")

    def report_once(self, text):
        if text not in self._diagnostics:
            self._diagnostics.add(text)
            self.log_message(self.label + " " + text)

    def build_midi_map(self, midi_map_handle):
        super().build_midi_map(midi_map_handle)
        if self.config and self.config["setup_mode"]:
            # No parameter mappings exist in setup mode. Capture only this input.
            for channel in range(16):
                for number in range(128):
                    if self.probe_cc:
                        Live.MidiMap.forward_midi_cc(
                            self._c_instance.handle(), midi_map_handle, channel, number)
                    if self.probe_notes:
                        Live.MidiMap.forward_midi_note(
                            self._c_instance.handle(), midi_map_handle, channel, number)

    def receive_midi(self, midi_bytes):
        if self.config and self.config["setup_mode"]:
            if len(midi_bytes) == 3:
                status, number, value = midi_bytes
                kind = status & 0xF0
                channel = (status & 0x0F) + 1
                if kind == 0xB0 and self.probe_cc:
                    self.log_message("{} MIDI CC channel={} cc={} value={}".format(
                        self.label, channel, number, value))
                elif kind in (0x80, 0x90) and self.probe_notes:
                    on = kind == 0x90 and value > 0
                    self.log_message("{} MIDI NOTE channel={} note={} value={} {}".format(
                        self.label, channel, number, value, "on" if on else "off"))
            return
        super().receive_midi(midi_bytes)


class HybridElectro(SetupSurface):
    def __init__(self, c_instance):
        self._knob_banks = {}
        self._bound = {}
        self._ticks = 0
        self._last_selected = None
        self._volume_bindings = {}
        super().__init__(c_instance)
        if self.config is None:
            return
        with self.component_guard():
            if not self.config["setup_mode"]:
                for bank_name in ("track", "global"):
                    bank = self.config["encoders"].get(bank_name)
                    if bank is None:
                        continue
                    knobs = []
                    mode = getattr(Live.MidiMap.MapMode, MAP_MODES[bank["mode"]])
                    for index, cc in enumerate(bank["cc"]):
                        knob = EncoderElement(MIDI_CC_TYPE, bank["channel"] - 1, cc, mode,
                                              name="{}_{}".format(bank_name, index + 1))
                        knob.set_needs_takeover(bank["mode"] == "absolute")
                        knob.mapping_sensitivity = bank.get("sensitivity", 1.0)
                        follow = self.config.get("volume_follow_session") or {}
                        if bank_name == "global" and follow.get("max_db") == 0:
                            self._volume_bindings[index] = UnityVolumeBinding()
                            knob.add_value_listener(partial(self._volume_value, index))
                        knobs.append(knob)
                    self._knob_banks[bank_name] = knobs
            self.register_slot(self.song().view, self._selection_changed, "selected_track")
            self._refresh()
        mode = ("SETUP: turn knobs; see Log.txt" if self.config["setup_mode"] else
                "v4 ready / TRACK={} / GLOBAL={}".format(
                    self.config.get("track_macros", {}).get("rack_name", "custom"),
                    "volumes/" + self.config["volume_follow_session"]["surface"]
                    if self.config.get("volume_follow_session") else "fixed"))
        self.log_message(self.label + " " + mode)
        self.show_message(self.label + " " + mode)
        if self._volume_bindings:
            self.log_message(self.label + " volume maximum: 0 dB / pickup")

    def build_midi_map(self, midi_map_handle):
        super().build_midi_map(midi_map_handle)
        if self.config and self.config["dump_parameters"]:
            for bank_name, knobs in self._knob_banks.items():
                for index, knob in enumerate(knobs):
                    parameter = self._bound.get((bank_name, index))
                    row = dict(bank=bank_name, knob=index + 1,
                               cc=self.config["encoders"][bank_name]["cc"][index],
                               selected_track=self.song().view.selected_track.name,
                               mapped=getattr(knob, "_is_mapped", None))
                    if parameter is not None:
                        row.update(name=parameter.name,
                                   original_name=getattr(parameter, "original_name", parameter.name),
                                   enabled=getattr(parameter, "is_enabled", None),
                                   value=parameter.value)
                    self.log_message(self.label + " MAPPING " + json.dumps(row, ensure_ascii=False))

    def _selection_changed(self):
        with self.component_guard():
            self._refresh()

    def _refresh(self):
        if self.config is None:
            return
        song = self.song()
        selected = song.view.selected_track
        if selected != self._last_selected:
            self._last_selected = selected
            if selected is not None:
                if self.config["dump_parameters"]:
                    for line in describe_track(selected):
                        self.log_message(self.label + " " + line)
                if not self.config["setup_mode"]:
                    self.show_message(self.label + " TRACK: " + selected.name)
        for bank_name, knobs in self._knob_banks.items():
            if bank_name == "global" and self.config.get("volume_follow_session"):
                self._refresh_volumes()
                continue
            targets = (self.config["global"] if bank_name == "global" else
                       track_targets(self.config, selected))
            for index, (knob, target) in enumerate(zip(knobs, targets)):
                try:
                    parameter = resolve_target(song, selected, target)
                except (TargetError, RuntimeError, AttributeError) as error:
                    parameter = None
                    self.report_once("{} knob {}: {}".format(bank_name, index + 1, error))
                key = (bank_name, index)
                self._bind(key, knob, parameter)

    def _bind(self, key, knob, parameter):
        if parameter != self._bound.get(key):
            # Live owns scaling and pickup. Binding never writes a value.
            knob.release_parameter()
            binding = self._volume_bindings.get(key[1]) if key[0] == "global" else None
            if binding is not None:
                binding.bind(parameter)
            elif parameter is not None:
                knob.connect_to(parameter)
            self._bound[key] = parameter

    def _volume_value(self, index, value):
        # Read the ring at the moment of input as well as on display ticks.
        # A turn immediately after a navigation event must use the new track.
        try:
            with self.component_guard():
                self._refresh_volumes()
            self._volume_bindings[index].receive(value)
        except (TargetError, AttributeError, RuntimeError) as error:
            self.report_once("volume knob {}: {}".format(index + 1, error))

    def _refresh_volumes(self):
        follow = self.config.get("volume_follow_session")
        if not follow:
            return
        surfaces = get_control_surfaces()
        for index, knob in enumerate(self._knob_banks.get("global", ())):
            try:
                track = resolve_session_track(surfaces, follow["surface"], index + 1)
                parameter = track.mixer_device.volume
            except (TargetError, AttributeError, RuntimeError) as error:
                parameter = None
                self.report_once("volume knob {}: {}".format(index + 1, error))
            try:
                self._bind(("global", index), knob, parameter)
            except (TargetError, AttributeError, RuntimeError) as error:
                # Invalid/deleted parameters must not keep an earlier target.
                binding = self._volume_bindings.get(index)
                if binding is not None:
                    binding.bind(None)
                self._bound[("global", index)] = None
                self.report_once("volume knob {}: {}".format(index + 1, error))

    def update_display(self):
        super().update_display()
        self._ticks += 1
        # Also catches device deletion, renaming, Rack edits, and track renaming.
        if self.config and self._ticks % 10 == 0:
            with self.component_guard():
                self._refresh()
        elif self.config and not self.config["setup_mode"] and self.config.get("volume_follow_session"):
            # Follow ring movement even when the selected track stays the same.
            with self.component_guard():
                self._refresh_volumes()

    def disconnect(self):
        self._knob_banks.clear()
        self._bound.clear()
        self._volume_bindings.clear()
        super().disconnect()
