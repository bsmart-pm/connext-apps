# Cloud Command Export Bundle

This folder is a self-contained export of the cloud command demo.

## Included files

- `cloud_command.py`
- `fleet_common.py`
- `setup_venv.sh`
- `run_cloud_command.sh`

## What you need on the new computer

- Python 3.10 or newer
- RTI Connext DDS 7.7.0 installed, or the matching Python wheel available locally

## Step-by-step setup

1. Copy this entire folder to the new computer.
2. Open Terminal and change into the folder.
3. If you have a local RTI Python wheel, set `RTI_CONNEXT_WHEEL` to that file path.
4. Run `./setup_venv.sh`.
5. If the script reports that the RTI bindings are missing, install RTI Connext DDS 7.7.0 and rerun step 4.
6. Run `./run_cloud_command.sh`.
7. A browser tab should open automatically with the dashboard.

## Notes

- The dashboard listens on `127.0.0.1` and picks a free port automatically.
- This bundle only contains the cloud dashboard side.
- For live telemetry, the drone side and any required gateway or bridge must already be running and publishing data on the expected DDS domains.