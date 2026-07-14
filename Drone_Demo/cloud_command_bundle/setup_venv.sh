#!/usr/bin/env bash
set -euo pipefail

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_dir="$bundle_dir/.venv"

if [[ ! -d "$venv_dir" ]]; then
	python3 -m venv "$venv_dir"
fi

source "$venv_dir/bin/activate"
python -m pip install --upgrade pip

if [[ -n "${RTI_CONNEXT_WHEEL:-}" ]]; then
	python -m pip install "$RTI_CONNEXT_WHEEL"
fi

python - <<'PY'
try:
	import rti.connextdds as dds
except Exception as exc:
	print("RTI Connext DDS Python bindings are not available yet.")
	print("Install RTI Connext DDS 7.7.0 or set RTI_CONNEXT_WHEEL to the wheel file, then rerun this script.")
	print(f"Import check failed: {exc}")
	raise SystemExit(1)
else:
	print("Python environment is ready.")
PY