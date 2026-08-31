#!/usr/bin/env python3
"""Which ESC/POS image dialect does this printer speak?

Run on the Pi (booth venv):  python test_print.py
Prints a labelled black bar for each mode — the label with a clean
solid rectangle under it (no garbage symbols) is the winner.
"""
from escpos.printer import Usb
from PIL import Image

# profile: a standard Epson 80mm/576px definition so escpos knows the
# paper width — the VRETTI has no profile of its own
p = Usb(0x1fc9, 0x2016, profile="TM-T88III")
img = Image.new("1", (384, 80), 0)

for impl in ("graphics", "bitImageColumn", "bitImageRaster"):
    p.text(f"--- {impl} ---\n")
    try:
        p.image(img, impl=impl)
    except Exception as e:
        p.text(f"(failed: {e})\n")
    p.text("\n")

p.text("\n\n")
p.cut()
p.close()
