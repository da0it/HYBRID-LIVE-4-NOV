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
    kinds = [key for key in ("mixer", "send", "device", "rack") if key in target]
    if len(kinds) != 1:
        raise ConfigurationError(label + ": choose exactly one of mixer, send, device, rack")
    kind = kinds[0]
    allowed = {"track", kind}
    if kind == "mixer":
        if target[kind] not in ("volume", "panning", "crossfader"):
            raise ConfigurationError(label + ": unknown mixer parameter")
    elif kind == "send":
        _integer(target[kind], 1, 128, label + ".send")
    elif kind == "rack":
        _name(target["rack"], label + ".rack")
        _integer(target.get("macro"), 1, 16, label + ".macro")
        allowed.add("macro")
    else:
        _name(target["device"], label + ".device")
        _name(target.get("parameter"), label + ".parameter")
        allowed.add("parameter")
        if "device_scope" in target:
            if target["device_scope"] != "top_level":
                raise ConfigurationError(label + ": device_scope must be top_level")
            allowed.add("device_scope")
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
    macros = config.get("track_macros")
    if macros is not None:
        if not isinstance(macros, dict) or set(macros) != {"rack_name"}:
            raise ConfigurationError("track_macros must contain only rack_name")
        _name(macros["rack_name"], "track_macros.rack_name")
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
    volumes = config.get("volume_follow_session")
    if volumes is not None:
        if (not isinstance(volumes, dict) or "surface" not in volumes
                or set(volumes) - {"surface", "max_db"}):
            raise ConfigurationError("volume_follow_session needs surface and optional max_db")
        _name(volumes["surface"], "volume_follow_session.surface")
        if encoders.get("global") is None:
            raise ConfigurationError("volume_follow_session requires encoders.global")
        if "max_db" in volumes:
            if type(volumes["max_db"]) not in (int, float) or volumes["max_db"] != 0:
                raise ConfigurationError("volume_follow_session.max_db must be 0")
            if encoders["global"]["mode"] != "absolute":
                raise ConfigurationError("The 0 dB volume range requires absolute encoders")
    pads = config.get("pads", [])
    if not isinstance(pads, list):
        raise ConfigurationError("pads must be a list")
    follow = config.get("pad_follow_session")
    if follow is not None:
        if not isinstance(follow, dict) or set(follow) != {"surface"}:
            raise ConfigurationError("pad_follow_session must contain only surface")
        _name(follow["surface"], "pad_follow_session.surface")
    pad_messages = set()
    for pad in pads:
        if not isinstance(pad, dict):
            raise ConfigurationError("pad must be an object")
        _integer(pad.get("channel"), 1, 16, "pad.channel")
        _integer(pad.get("note"), 0, 127, "pad.note")
        if follow:
            _integer(pad.get("track_slot"), 1, 8, "pad.track_slot")
        else:
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


def resolve_session_track(surfaces, surface_name, slot):
    """Read the same SessionComponent that draws the source controller's ring."""
    source = _one((s for s in surfaces if
                   surface_name in (type(s).__name__, type(s).__module__.split(".")[0])),
                  "control surface " + surface_name)
    session = getattr(source, "_highlighting_session_component", None)
    if session is None:
        raise TargetError(surface_name + " has no active session ring")
    if not 1 <= slot <= session.width():
        raise TargetError("Pad is outside the session ring")
    tracks = tuple(session.tracks_to_use())
    index = session.track_offset() + slot - 1
    if not 0 <= index < len(tracks):
        raise TargetError("No track at this pad in the session ring")
    return tracks[index]


def walk_devices(container, prefix=()):
    """Include all Rack chains, even when a chain is not selected in the UI."""
    for device in getattr(container, "devices", ()):
        path = prefix + (device.name,)
        yield path, device
        for chain in getattr(device, "chains", ()):
            yield from walk_devices(chain, path + (chain.name,))


def track_targets(config, selected_track):
    """Follow any selected track, including tracks created after initialization."""
    if selected_track is None:
        return [None] * 8
    if selected_track.name in config["tracks"]:
        return config["tracks"][selected_track.name]
    macros = config.get("track_macros")
    if macros:
        return [{"rack": macros["rack_name"], "macro": i + 1} for i in range(8)]
    return [None] * 8


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
    elif "rack" in target:
        rack = _one((d for _, d in walk_devices(track) if d.name == target["rack"]),
                    "{} / Rack {}".format(track.name, target["rack"]))
        if not getattr(rack, "can_have_chains", False):
            raise TargetError("{} is not a Rack".format(target["rack"]))
        original = "Macro {}".format(target["macro"])
        parameter = _one((p for p in rack.parameters
                          if getattr(p, "original_name", p.name) == original),
                         "{} / {} / {}".format(track.name, rack.name, original))
    else:
        devices = (track.devices if target.get("device_scope") == "top_level"
                   else (device for _, device in walk_devices(track)))
        device = _one((device for device in devices if device.name == target["device"]),
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
