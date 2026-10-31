"""AKAI input: note-on selects a named track, with no arm/transport actions."""

from functools import partial

from _Framework.ButtonElement import ButtonElement
from _Framework.InputControlElement import MIDI_NOTE_TYPE

from .core import TargetError, resolve_track
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
                button.add_value_listener(partial(self._select, pad["track"]))
                self._buttons.append(button)

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
