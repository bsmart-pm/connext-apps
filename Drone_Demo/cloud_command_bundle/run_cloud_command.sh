#!/usr/bin/env bash
set -euo pipefail

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -x "$bundle_dir/.venv/bin/python" ]]; then
	echo "No local virtual environment found. Run setup_venv.sh first."
	exit 1
fi

exec "$bundle_dir/.venv/bin/python" "$bundle_dir/cloud_command.py"