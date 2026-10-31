#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 tools/midi_log.py --follow
result=$?
if (( result != 0 )); then
  read '?Press Enter to close...'
fi
exit $result
