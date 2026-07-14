# Cloud Command Export Bundle

This folder is a self-contained export of the cloud command demo.

## Included files

- `cloud_command.py`
- `fleet_common.py`
- `setup_venv.sh`
- `run_cloud_command.sh`

## What you need on the new computer

- Python 3.10 or newer
- RTI Connext DDS 7.7.0 installed on the machine, or the matching Python wheel available locally
- A valid RTI license file available through `RTI_LICENSE_FILE`

## Step-by-step setup

1. Copy this entire folder to the new computer.
2. Open Terminal and change into the folder.
3. If you have a local RTI Python wheel, set `RTI_CONNEXT_WHEEL` to that file path.
4. Run `./setup_venv.sh`.
5. If the script reports that the import failed even though pip said the wheel was already installed, the machine still needs the native RTI Connext runtime and/or the RTI library path and license file configured.
6. Run `./run_cloud_command.sh`.
7. A browser tab should open automatically with the dashboard.

## Install RTI Connext into the bundle venv

If you already have the RTI Connext Python wheel on disk, install it directly into the bundle virtual environment:

```sh
cd /path/to/cloud_command_bundle
./setup_venv.sh
source .venv/bin/activate
export RTI_CONNEXT_WHEEL=/path/to/rti_connext-7.7.0-<platform>.whl
python -m pip install "$RTI_CONNEXT_WHEEL"
python -c "import rti.connextdds as dds; print(dds.__file__)"
```

If you installed RTI Connext DDS from the full RTI distribution instead of using a wheel, make sure the Connext environment is active before launching the demo. On macOS that usually means setting `RTI_LICENSE_FILE` and adding the RTI `lib` directory to `DYLD_LIBRARY_PATH` or sourcing RTI's environment setup script.

## Notes

- The dashboard listens on `127.0.0.1` and picks a free port automatically.
- This bundle only contains the cloud dashboard side.
- For live telemetry, the drone side and any required gateway or bridge must already be running and publishing data on the expected DDS domains.
- On macOS, you may need to source RTI's environment script or add the RTI `lib` directory to `DYLD_LIBRARY_PATH` before running the demo.