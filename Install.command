#!/bin/zsh
cd -- "${0:A:h}" || exit 1
python3 tools/install.py
result=$?
if (( result != 0 )); then
  print 'Installation failed. See the error above.'
fi
read '?Press Enter to close...'
exit $result
