#!/usr/bin/env bash
# Activate the virtual environment and start the receiver.
cd "$(dirname "$0")" || exit 1
# shellcheck disable=SC1091
source venv/bin/activate
python receiver/rtl_sdr_receiver.py "$@"
