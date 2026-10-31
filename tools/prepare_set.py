#!/usr/bin/env python3
"""Copy a Live project and remove only MIDI mappings reserved by HybridElectro."""

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from HybridElectro.core import load_config


def clean_midi_mappings(root, config):
    cc_messages = {(bank["channel"] - 1, cc)
                   for bank in config["encoders"].values() if bank
                   for cc in bank["cc"]}
    note_messages = {(pad["channel"] - 1, pad["note"]) for pad in config["pads"]}
    removed = []
    for parent in root.iter():
        for key in list(parent.findall("KeyMidi")):
            fields = {child.tag: child.get("Value") for child in key}
            if fields.get("PersistentKeyString"):
                continue
            try:
                channel, number = int(fields["Channel"]), int(fields["NoteOrController"])
            except (KeyError, TypeError, ValueError):
                continue
            is_note = fields.get("IsNote") == "true"
            reserved = note_messages if is_note else cc_messages
            if (channel, number) not in reserved:
                continue
            # A note-range mapping can include unreserved keys: never remove it wholesale.
            if is_note and any(fields.get(name, "-1") not in ("-1", str(number))
                               for name in ("LowerRangeNote", "UpperRangeNote")):
                raise ValueError("Reserved note intersects a note-range mapping; inspect it in Live")
            removed.append({"parameter": parent.tag, "channel": channel + 1,
                            "type": "note" if is_note else "cc", "number": number})
            parent.remove(key)
    return removed


def prepare(source, destination, config):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Choose a new output folder: " + str(destination))
    if source.parent == destination or source.parent in destination.parents:
        raise ValueError("Output must be outside the original project")
    original = source.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    root = ET.fromstring(gzip.decompress(original))
    values = [ET.tostring(node) for node in root.iter("Manual")]
    removed = clean_midi_mappings(root, config)
    assert values == [ET.tostring(node) for node in root.iter("Manual")]
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    ET.fromstring(xml)

    def ignore(folder, names):
        excluded = {name for name in names if name in ("Backup", ".DS_Store")}
        if Path(folder) == source.parent:
            excluded.add(source.name)
        return excluded

    shutil.copytree(source.parent, destination, ignore=ignore)
    output = destination / "HybridElectro.als"
    output.write_bytes(gzip.compress(xml, mtime=0))
    if source.read_bytes() != original:
        raise RuntimeError("Original Set was saved during preparation; prepare a new copy")
    report = {"source": str(source), "source_sha256": original_hash,
              "prepared_set": str(output), "manual_values_preserved": len(values),
              "removed_mappings": removed}
    (destination / "HybridElectro preparation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = prepare(args.source, args.destination, load_config(ROOT / "HybridElectro/config.json"))
    print(json.dumps(result, indent=2, ensure_ascii=False))
