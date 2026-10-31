"""AKAI input: select a track in the Launchpad ring, or a fixed named track."""

from functools import partial

from _Framework.ButtonElement import ButtonElement
from _Framework.ControlSurface import get_control_surfaces
from _Framework.InputControlElement import MIDI_NOTE_TYPE

from .core import TargetError, resolve_track, resolve_session_track
from .surface import SetupSurface


class HybridElectroPads(SetupSurface):
    label = "HybridElectroPads"
    probe_cc = False
    probe_notes = True

    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._buttons = []
        if self.config is None:
            return
        if self.config["setup_mode"]:
            self.show_message(self.label + " SETUP: press selection pads; see Log.txt")
            return
        with self.component_guard():
            for index, pad in enumerate(self.config["pads"]):
                button = ButtonElement(True, MIDI_NOTE_TYPE, pad["channel"] - 1,
                                       pad["note"], name="Select_{}".format(index + 1))
                follow = self.config.get("pad_follow_session")
                callback = (partial(self._select_slot, pad["track_slot"]) if follow else
                            partial(self._select, pad["track"]))
                button.add_value_listener(callback)
                self._buttons.append(button)
        self.log_message(self.label + " ready / " + (
            "follows " + self.config["pad_follow_session"]["surface"]
            if self.config.get("pad_follow_session") else "fixed tracks"))

    def _select_slot(self, slot, value):
        if value == 0:
            return
        try:
            track = resolve_session_track(get_control_surfaces(),
                                          self.config["pad_follow_session"]["surface"], slot)
        except (TargetError, AttributeError, RuntimeError) as error:
            self.report_once(str(error))
            self.show_message(self.label + ": " + str(error))
            return
        self.song().view.selected_track = track
        self.show_message(self.label + " selected: " + track.name)

    def _select(self, name, value):
        if value == 0:
            return
        try:
            track = resolve_track(self.song(), name)
        except TargetError as error:
            self.report_once(str(error))
            self.show_message(self.label + ": " + str(error))
            return
        self.song().view.selected_track = track
        self.show_message(self.label + " selected: " + track.name)
