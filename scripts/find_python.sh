#!/usr/bin/env bash
# Print a working Python 3 interpreter (python3, else python: on Windows runners `python3` can
# be the Microsoft Store alias, which does not run).
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    command -v "$candidate"
    exit 0
  fi
done
echo "no Python 3.11+ found" >&2
exit 1
