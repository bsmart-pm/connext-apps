#!/usr/bin/env sh

if [ -f "./.venv/bin/activate" ]; then
  # Source the repository-local virtual environment.
  . "./.venv/bin/activate"
else
  echo "No virtual environment found at ./.venv" >&2
  return 1 2>/dev/null || exit 1
fi
