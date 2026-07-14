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
import os

try:
	import rti.connextdds as dds
except ModuleNotFoundError as exc:
	if getattr(exc, "name", "") == "rti":
		print("RTI Connext DDS Python bindings are not installed in this virtual environment.")
		print("Set RTI_CONNEXT_WHEEL to the matching wheel file, or install the RTI Connext DDS Python package into this venv, then rerun this script.")
		print(f"Import check failed: {exc}")
		raise SystemExit(1)
	print("RTI Connext DDS could not be imported from this virtual environment.")
	print("The import failed before the package could load, which usually means the RTI runtime or its native libraries are missing from the machine.")
	print("Install the RTI Connext DDS distribution on this machine, set RTI_LICENSE_FILE to a valid license file, and make sure the RTI library path is exported for your platform.")
	print("On macOS, that usually means sourcing RTI's environment setup script or adding $NDDSHOME/lib/<architecture> to DYLD_LIBRARY_PATH.")
	if os.environ.get("RTI_CONNEXT_WHEEL"):
		print(f"RTI_CONNEXT_WHEEL was set to: {os.environ['RTI_CONNEXT_WHEEL']}")
	print(f"Import check failed: {exc}")
	raise SystemExit(1)
except Exception as exc:
	print("RTI Connext DDS could not be imported from this virtual environment.")
	print("If pip reported 'requirement already satisfied', the Python wheel is already installed and the missing piece is usually the native RTI Connext runtime or its library path.")
	print("Install the RTI Connext DDS distribution on this machine, set RTI_LICENSE_FILE to a valid license file, and make sure the RTI library path is exported for your platform.")
	print("On macOS, that usually means sourcing RTI's environment setup script or adding $NDDSHOME/lib/<architecture> to DYLD_LIBRARY_PATH.")
	if os.environ.get("RTI_CONNEXT_WHEEL"):
		print(f"RTI_CONNEXT_WHEEL was set to: {os.environ['RTI_CONNEXT_WHEEL']}")
	print(f"Import check failed: {exc}")
	raise SystemExit(1)
else:
	if not os.environ.get("RTI_LICENSE_FILE"):
		print("Warning: RTI_LICENSE_FILE is not set. The demo may still fail later until it points to a valid RTI license file.")
	print("Python environment is ready.")
PY