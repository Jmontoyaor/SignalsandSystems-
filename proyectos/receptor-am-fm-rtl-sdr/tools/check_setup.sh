#!/usr/bin/env bash
# Check step by step that everything needed by the RTL-SDR receiver is ready.
# Usage: tools/check_setup.sh   (from any directory)

cd "$(dirname "$0")/.." || exit 1

ok()      { printf '  [OK]      %s\n' "$1"; }
missing() { printf '  [MISSING] %s\n            -> %s\n' "$1" "$2"; }

echo "== 1. System packages =="
for p in rtl-sdr librtlsdr-dev python3-tk python3-venv; do
  if dpkg -s "$p" >/dev/null 2>&1; then ok "$p"; else missing "$p" "sudo apt install -y $p"; fi
done

echo "== 2. RTL-SDR dongle connected (lsusb) =="
if lsusb | grep -qiE '0bda:2838|0bda:2832|RTL28'; then
  ok "$(lsusb | grep -iE '0bda:2838|0bda:2832|RTL28')"
else
  missing "RTL-SDR not found" "plug in the USB dongle and run this script again"
fi

echo "== 3. Python virtual environment =="
if [ -x venv/bin/python ]; then ok "venv exists"; else missing "venv" "python3 -m venv venv && venv/bin/pip install -r requirements.txt"; fi
# shellcheck disable=SC1091
source venv/bin/activate 2>/dev/null

echo "== 4. Python libraries =="
python -c "import pkg_resources" 2>/dev/null && ok "pkg_resources (setuptools < 81)" || missing "pkg_resources" 'pip install "setuptools<81"'
python -c "import tkinter" 2>/dev/null && ok "tkinter (plot windows)" || missing "tkinter" "sudo apt install -y python3-tk"
python -c "import numpy, scipy, matplotlib" 2>/dev/null && ok "numpy, scipy, matplotlib" || missing "numpy/scipy/matplotlib" "pip install -r requirements.txt"
python -c "import sounddevice" 2>/dev/null && ok "sounddevice (audio output)" || missing "sounddevice" "pip install sounddevice"
python -c "from rtlsdr import RtlSdr" 2>/dev/null && ok "pyrtlsdr imports correctly" || missing "pyrtlsdr / librtlsdr" "sudo apt install -y rtl-sdr librtlsdr-dev"

echo "== 5. Driver test (rtl_test, 3 seconds) =="
if command -v rtl_test >/dev/null; then
  timeout 3 rtl_test 2>&1 | head -8
else
  missing "rtl_test" "sudo apt install -y rtl-sdr"
fi
