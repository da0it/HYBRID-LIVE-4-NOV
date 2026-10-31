#!/usr/bin/env python3
"""Install both surfaces, backing up any existing versions before copying."""

import argparse
import datetime
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("HybridElectro", "HybridElectroPads")


def install(library, replace_config=False):
    destination = Path(library).expanduser().resolve() / "Remote Scripts"
    source_config = ROOT / "HybridElectro" / "config.json"
    import sys
    sys.path.insert(0, str(ROOT))
    from HybridElectro.core import load_config
    load_config(source_config)
    if destination == ROOT or ROOT in destination.parents or destination in ROOT.parents:
        raise ValueError("Installation destination must be outside the source project")
    # Validate everything before making any filesystem changes.
    for name in PACKAGES:
        if not (ROOT / name / "__init__.py").is_file():
            raise FileNotFoundError(ROOT / name / "__init__.py")
    existing_config = destination / "HybridElectro" / "config.json"
    preserved = (existing_config.read_bytes()
                 if existing_config.exists() and not replace_config else None)
    if preserved is not None:
        load_config(existing_config)
    backups = []
    for name in PACKAGES:
        current = destination / name
        if current.is_symlink():
            raise ValueError("Refusing to overwrite a symbolic link: " + str(current))
        if current.exists() and not current.is_dir():
            raise ValueError("Expected a directory: " + str(current))
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_root = destination.parent / "HybridElectro Backups" / timestamp
    for name in PACKAGES:
        current = destination / name
        if current.exists():
            backup = backup_root / name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(current, backup)
            backups.append(backup)
    for name in PACKAGES:
        shutil.copytree(ROOT / name, destination / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    if preserved is not None:
        existing_config.write_bytes(preserved)
        print("Existing config.json preserved. Use --replace-config to install the project config.")
    print("Installed: " + str(destination))
    for backup in backups:
        print("Backup: " + str(backup))
    print("Restart Ableton Live. Settings > Link, Tempo & MIDI > Control Surface.")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path,
                        default=Path.home() / "Music" / "Ableton" / "User Library",
                        help="Your Ableton User Library folder")
    parser.add_argument("--replace-config", action="store_true",
                        help="Replace installed config after backing it up")
    args = parser.parse_args()
    install(args.library, args.replace_config)


if __name__ == "__main__":
    main()
