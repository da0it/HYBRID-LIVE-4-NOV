#!/usr/bin/env python3
"""Read CC messages directly from macOS CoreMIDI without sending any MIDI."""

import argparse
import ctypes as C
import json
import platform
import queue
import time
from pathlib import Path


def monitor(seconds, output):
    midi = C.CDLL('/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI')
    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cf.CFStringCreateWithCString.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32]
    cf.CFStringCreateWithCString.restype = C.c_void_p
    cf.CFStringGetCString.argtypes = [C.c_void_p, C.c_char_p, C.c_long, C.c_uint32]
    cf.CFRelease.argtypes = [C.c_void_p]
    midi.MIDIGetNumberOfSources.restype = C.c_size_t
    midi.MIDIGetSource.argtypes = [C.c_size_t]
    midi.MIDIGetSource.restype = C.c_uint32
    midi.MIDIObjectGetStringProperty.argtypes = [C.c_uint32, C.c_void_p, C.POINTER(C.c_void_p)]
    midi.MIDIClientCreate.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p, C.POINTER(C.c_uint32)]
    read_type = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_void_p)
    midi.MIDIInputPortCreate.argtypes = [C.c_uint32, C.c_void_p, read_type, C.c_void_p, C.POINTER(C.c_uint32)]
    midi.MIDIPortConnectSource.argtypes = [C.c_uint32, C.c_uint32, C.c_void_p]
    midi.MIDIPortDispose.argtypes = [C.c_uint32]
    midi.MIDIClientDispose.argtypes = [C.c_uint32]
    events = queue.SimpleQueue()
    sources = {}

    def string(value):
        return cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)

    def check(status):
        if status:
            raise RuntimeError('CoreMIDI error {}'.format(status))

    @read_type
    def callback(packet_list, context, source):
        count = C.c_uint32.from_address(packet_list).value
        address = packet_list + 4
        for _ in range(count):
            length = C.c_uint16.from_address(address + 8).value
            events.put((int(source or 0), C.string_at(address + 10, length)))
            address += 10 + length
            if platform.machine() in ('arm64', 'aarch64'):
                address = (address + 3) & ~3

    client, port = C.c_uint32(), C.c_uint32()
    label = string('HybridElectro read-only probe')
    key = string('displayName')
    try:
        check(midi.MIDIClientCreate(label, None, None, C.byref(client)))
        check(midi.MIDIInputPortCreate(client, label, callback, None, C.byref(port)))
        for index in range(midi.MIDIGetNumberOfSources()):
            endpoint = midi.MIDIGetSource(index)
            name = C.c_void_p()
            check(midi.MIDIObjectGetStringProperty(endpoint, key, C.byref(name)))
            buffer = C.create_string_buffer(1024)
            cf.CFStringGetCString(name, buffer, 1024, 0x08000100)
            cf.CFRelease(name)
            text = buffer.value.decode()
            if 'SMC' in text.upper() or 'SINCO' in text.upper():
                sources[index + 1] = text
                check(midi.MIDIPortConnectSource(port, endpoint, C.c_void_p(index + 1)))
                print('Listening: ' + text, flush=True)
        if not sources:
            raise RuntimeError('No SMC-PAD MIDI sources found')
        output.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + seconds
        states = {}
        with output.open('w', encoding='utf-8') as stream:
            while time.monotonic() < deadline:
                try:
                    source, data = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                status, values = states.get(source, (None, []))
                for byte in data:
                    if byte >= 0xF8:
                        continue
                    if byte & 0x80:
                        status = byte if byte < 0xF0 else None
                        values = []
                    elif status is not None:
                        values.append(byte)
                        size = 1 if status & 0xF0 in (0xC0, 0xD0) else 2
                        if len(values) == size:
                            if status & 0xF0 == 0xB0:
                                row = dict(time=time.time(), source=sources[source],
                                           channel=(status & 15) + 1, cc=values[0], value=values[1])
                                line = json.dumps(row, ensure_ascii=False)
                                stream.write(line + '\n')
                                stream.flush()
                                print(line, flush=True)
                            values = []
                states[source] = status, values
    finally:
        if port.value:
            midi.MIDIPortDispose(port)
        if client.value:
            midi.MIDIClientDispose(client)
        cf.CFRelease(label)
        cf.CFRelease(key)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--output', type=Path, default=Path('output/midi-probe.jsonl'))
    args = parser.parse_args()
    try:
        monitor(args.seconds, args.output)
    except KeyboardInterrupt:
        pass
