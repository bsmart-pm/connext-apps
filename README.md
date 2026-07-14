# Connext Demos

This repository collects local Connext-related demo assets under one parent folder while keeping `rticonnextdds-examples` as a separate repository boundary.

## Layout

- `Drone_Demo/` - local demo code
- `rticonnextdds-examples/` - separate git repository, tracked here as its own boundary
- `rti_license.dat` - RTI license file used by local examples
- `activate_venv.sh` - helper script to activate the root virtual environment
- `.venv/` - local Python virtual environment, not meant to be committed

## Python environment

From the repository root, activate the local virtual environment with:

```sh
source ./activate_venv.sh
```

The script checks for `./.venv/bin/activate` and sources it in the current shell. If the venv is missing, it prints an error and exits.

## RTI license file

For Connext examples, the simplest setup is to point the runtime at the license file in this repository root:

```sh
export RTI_LICENSE_FILE=/Users/bsmart/githubrepos/Connext_Demos/rti_license.dat
```

You can put that line in your shell profile, such as `~/.zshrc`, if you want it to apply automatically in new terminals.

## Working with rticonnextdds-examples

That folder is intended to stay as a separate repository boundary. To update or clone it on a new machine, use the upstream repo directly and initialize its submodule dependencies from inside that repository:

```sh
cd rticonnextdds-examples
git submodule update --init --recursive
```

If you clone the upstream examples repo directly, use:

```sh
git clone --recurse-submodule https://github.com/rticommunity/rticonnextdds-examples.git
```

## Running a local demo

Typical flow from the root:

```sh
source ./activate_venv.sh
export RTI_LICENSE_FILE=/Users/bsmart/githubrepos/Connext_Demos/rti_license.dat
python Drone_Demo/cloud_command.py
```

Adjust the script name for the demo you want to run.
