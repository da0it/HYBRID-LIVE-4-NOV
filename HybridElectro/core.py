"""Configuration and exact parameter lookup; usable without Ableton installed."""

import json
import math


class ConfigurationError(ValueError):
    pass


class TargetError(LookupError):
    pass


MAP_MODES = {
    "absolute": "absolute",
    "relative_two_complement": "relative_smooth_two_compliment",
    "relative_signed_bit": "relative_smooth_signed_bit",
    "relative_binary_offset": "relative_smooth_binary_offset",
}


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ConfigurationError("{} must be an integer {}..{}".format(label, low, high))


def _name(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("{} must be a nonempty string".format(label))


def validate_target(target, label, global_target=False):
    if target is None:
        return
    if not isinstance(target, dict):
        raise ConfigurationError("{} must be an object or null".format(label))
    if global_target and "track" not in target:
        raise ConfigurationError("{} needs a fixed track".format(label))
    if "track" in target:
        _name(target["track"], label + ".track")
        if target["track"].startswith("@"):
            selector = target["track"]
            if selector != "@master":
                parts = selector.split(":")
                if (len(parts) != 2 or parts[0] != "@return"
                        or not parts[1].isdigit() or int(parts[1]) < 1):
                    raise ConfigurationError(label + ": use @master or @return:1, @return:2, ...")
    kinds = [key for key in ("mixer", "send", "device") if key in target]
    if len(kinds) != 1:
        raise ConfigurationError(label + ": choose exactly one of mixer, send, device")
    kind = kinds[0]
    allowed = {"track", kind}
    if kind == "mixer":
        if target[kind] not in ("volume", "panning", "crossfader"):
            raise ConfigurationError(label + ": unknown mixer parameter")
    elif kind == "send":
        _integer(target[kind], 1, 128, label + ".send")
    else:
        _name(target["device"], label + ".device")
        _name(target.get("parameter"), label + ".parameter")
        allowed.add("parameter")
    unknown = set(target) - allowed
    if unknown:
        raise ConfigurationError(label + ": unknown fields " + ", ".join(sorted(unknown)))


def validate_config(config):
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ConfigurationError("config must be an object with version: 1")
    for flag in ("setup_mode", "dump_parameters"):
        if type(config.get(flag)) is not bool:
            raise ConfigurationError(flag + " must be true or false")
    encoders = config.get("encoders")
    if not isinstance(encoders, dict) or not isinstance(encoders.get("track"), dict):
        raise ConfigurationError("encoders.track is required")
    messages = set()
    for bank_name in ("track", "global"):
        bank = encoders.get(bank_name)
        if bank is None:
            continue
        if not isinstance(bank, dict):
            raise ConfigurationError("encoders." + bank_name + " must be an object or null")
        _integer(bank.get("channel"), 1, 16, bank_name + ".channel")
        ccs = bank.get("cc")
        if not isinstance(ccs, list) or len(ccs) != 8:
            raise ConfigurationError(bank_name + ".cc needs exactly eight CC numbers")
        for cc in ccs:
            _integer(cc, 0, 127, bank_name + ".cc")
            key = (bank["channel"], cc)
            if key in messages:
                raise ConfigurationError("Duplicate encoder MIDI channel/CC: {}".format(key))
            messages.add(key)
        if bank.get("mode") not in MAP_MODES:
            raise ConfigurationError(bank_name + ".mode: choose " + ", ".join(MAP_MODES))
        sensitivity = bank.get("sensitivity", 1.0)
        if (type(sensitivity) not in (int, float)
                or not math.isfinite(sensitivity) or sensitivity <= 0):
            raise ConfigurationError(bank_name + ".sensitivity must be finite and positive")
    tracks = config.get("tracks")
    if not isinstance(tracks, dict):
        raise ConfigurationError("tracks must be an object")
    for name, targets in tracks.items():
        _name(name, "track name")
        if not isinstance(targets, list) or len(targets) != 8:
            raise ConfigurationError(name + " needs eight targets (null = unused)")
        for i, target in enumerate(targets):
            validate_target(target, "{} knob {}".format(name, i + 1))
    targets = config.get("global")
    if not isinstance(targets, list) or len(targets) != 8:
        raise ConfigurationError("global needs eight targets (null = unused)")
    for i, target in enumerate(targets):
        validate_target(target, "global knob {}".format(i + 1), global_target=True)
    pads = config.get("pads", [])
    if not isinstance(pads, list):
        raise ConfigurationError("pads must be a list")
    pad_messages = set()
    for pad in pads:
        if not isinstance(pad, dict):
            raise ConfigurationError("pad must be an object")
        _integer(pad.get("channel"), 1, 16, "pad.channel")
        _integer(pad.get("note"), 0, 127, "pad.note")
        _name(pad.get("track"), "pad.track")
        key = (pad["channel"], pad["note"])
        if key in pad_messages:
            raise ConfigurationError("Duplicate pad MIDI channel/note: {}".format(key))
        pad_messages.add(key)
    return config


def load_config(path):
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ConfigurationError("Duplicate JSON key: " + key)
            obj[key] = value
        return obj

    with open(path, encoding="utf-8") as stream:
        return validate_config(json.load(stream, object_pairs_hook=unique_object))


def _one(matches, label):
    matches = list(matches)
    if not matches:
        raise TargetError("Not found: " + label)
    if len(matches) > 1:
        raise TargetError("Ambiguous name (rename duplicates): " + label)
    return matches[0]


def resolve_track(song, selector):
    if selector == "@master":
        return song.master_track
    if selector.startswith("@return:"):
        index = int(selector.split(":", 1)[1]) - 1
        if not 0 <= index < len(song.return_tracks):
            raise TargetError("Not found: " + selector)
        return song.return_tracks[index]
    return _one((track for track in tuple(song.tracks) + tuple(song.return_tracks)
                 if track.name == selector), "track " + selector)


def walk_devices(container, prefix=()):
    """Include all Rack chains, even when a chain is not selected in the UI."""
    for device in getattr(container, "devices", ()):
        path = prefix + (device.name,)
        yield path, device
        for chain in getattr(device, "chains", ()):
            yield from walk_devices(chain, path + (chain.name,))


def resolve_target(song, selected_track, target):
    if target is None:
        return None
    track = resolve_track(song, target["track"]) if "track" in target else selected_track
    if track is None:
        raise TargetError("No selected track")
    if "mixer" in target:
        parameter = getattr(track.mixer_device, target["mixer"], None)
        if parameter is None:
            raise TargetError("Not found: {} / {}".format(track.name, target["mixer"]))
    elif "send" in target:
        index = target["send"] - 1
        sends = track.mixer_device.sends
        if index >= len(sends):
            raise TargetError("Not found: {} / send {}".format(track.name, index + 1))
        parameter = sends[index]
    else:
        device = _one((device for _, device in walk_devices(track)
                       if device.name == target["device"]),
                      "{} / device {}".format(track.name, target["device"]))
        name = target["parameter"]
        parameter = _one((p for p in device.parameters
                          if name in (p.name, getattr(p, "original_name", p.name))),
                         "{} / {} / {}".format(track.name, device.name, name))
    return parameter


def describe_track(track):
    lines = ["PARAMETERS track=" + track.name]
    for path, device in walk_devices(track):
        parameters = [{"name": p.name, "original_name": getattr(p, "original_name", p.name)}
                      for p in device.parameters]
        lines.append(json.dumps({"path": list(path), "device": device.name,
                                 "parameters": parameters}, ensure_ascii=False))
    return lines
