# Cloud Command Export Bundle

This folder is a self-contained export of the cloud command demo.

## Included files

- `cloud_command.py`
- `fleet_common.py`
- `run_cloud_command.sh`
- `rti_license.dat`

## What you need on the new computer

- Python 3.10 or newer
- The RTI Connext DDS Python wheel for your platform, available locally
- A valid RTI license file is already included in this bundle as `rti_license.dat`

## Step-by-step setup

1. Copy this entire folder to the new computer.
2. Open Terminal and change into the folder.
3. Create a local virtual environment in this folder, for example `python3 -m venv .venv`.
4. Activate that virtual environment with `source .venv/bin/activate`.
5. Install the RTI Connext Python wheel into that virtual environment with `python -m pip install /path/to/rti_connext-7.7.0-<platform>.whl`.
6. Point `RTI_LICENSE_FILE` at the bundled license file, for example `export RTI_LICENSE_FILE="$PWD/rti_license.dat"`.
7. Run `./run_cloud_command.sh`.
8. A browser tab should open automatically with the dashboard.

## Connext Cloud
Connext Cloud Connection, requires 7.7 installation with a license file in the installation location of 7.7

1. Install RTI Connext Cloud CLI, brew tap realtimeinnovations/tap && brew install rticloud
2. Run rticloud configure
3. Run rticloud login
4. Run rticloud gateway command


## Notes

- The dashboard listens on `127.0.0.1` and picks a free port automatically.
- This bundle only contains the cloud dashboard side.
- For live telemetry, the drone side and any required gateway or bridge must already be running and publishing data on the expected DDS domains.
- On macOS, you may need to source RTI's environment script or add the RTI `lib` directory to `DYLD_LIBRARY_PATH` before running the demo.