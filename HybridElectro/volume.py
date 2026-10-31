"""Bounded volume input. Binding is read-only; only MIDI input writes a value."""

import math
import re

from .core import TargetError


class UnityVolumeBinding:
    """Absolute CC with pickup over the native fader range ending at unity."""

    def __init__(self):
        self.parameter = None
        self.minimum = None
        self.maximum = None
        self._previous_cc = None
        self._picked_up = False
        self._written = None

    def bind(self, parameter):
        if parameter == self.parameter:
            return
        self.parameter = None
        self._previous_cc = None
        self._picked_up = False
        self._written = None
        if parameter is None:
            return
        minimum = float(parameter.min)
        maximum = float(parameter.default_value)
        # Track Volume's native default is unity. Check the actual parameter
        # rather than hardcoding a normalized fader position or a dB curve.
        label = parameter.str_for_value(maximum).strip().replace(
            "\N{MINUS SIGN}", "-").replace(",", ".")
        match = re.fullmatch(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*dB", label, re.IGNORECASE)
        if (not all(math.isfinite(v) for v in (minimum, maximum, float(parameter.max)))
                or not minimum < maximum <= parameter.max
                or match is None or float(match.group(1)) != 0):
            raise TargetError("Cannot verify the Track Volume 0 dB endpoint")
        self.minimum, self.maximum = minimum, maximum
        self.parameter = parameter

    def receive(self, cc_value):
        parameter = self.parameter
        if parameter is None or not parameter.is_enabled:
            return
        value = max(0, min(127, int(cc_value)))
        current = parameter.value
        # Mouse edits/automation can move the fader after pickup, too.
        if self._written is not None and abs(current - self._written) > 1e-6:
            self._picked_up = False
            self._previous_cc = None
        target = max(0.0, min(127.0,
                     127.0 * (current - self.minimum) / (self.maximum - self.minimum)))
        if not self._picked_up:
            close = abs(value - target) <= 0.5
            crossed = (self._previous_cc is not None
                       and min(self._previous_cc, value) <= target
                       <= max(self._previous_cc, value))
            self._picked_up = close or crossed
        self._previous_cc = value
        if not self._picked_up:
            return
        # No intermediate value above unity is ever sent to the audio engine.
        result = self.minimum + (self.maximum - self.minimum) * value / 127.0
        parameter.value = min(self.maximum, max(self.minimum, result))
        self._written = parameter.value
