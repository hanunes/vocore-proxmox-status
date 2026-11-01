#!/usr/bin/env python3
"""Check a VoCore screen before installing anything. Needs python3-usb; run as root.

    python3 panel_test.py          red, green and blue layout pattern
    python3 panel_test.py 480 854  same, for another panel size

Expected on a 480x800 panel held wide: mostly blue, a green strip along one long
edge and a red block at one short end. Diagonal stripes mean the size is wrong.
"""
import sys

import usb.core

LINE, LINES = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) == 3 else (480, 800)
RED, GREEN, BLUE = b"\x00\xF8", b"\xE0\x07", b"\x1F\x00"

frame = b"".join(GREEN * 40 + (RED if y < LINES // 8 else BLUE) * (LINE - 40)
                 for y in range(LINES))
dev = usb.core.find(idVendor=0xC872, idProduct=0x1004)
if dev is None:
    sys.exit("No VoCore screen found (USB ID c872:1004). Check lsusb and the cable.")
dev.set_configuration()
cmd = lambda data: dev.ctrl_transfer(0x40, 0xB0, 0, 0, data, 100)
cmd([0x00, 0x29])                                  # wake: the panel boots asleep
cmd([0x00, 0x51, 0x02, 0, 0, 0, 50, 0])            # brightness
cmd([0x00, 0x2C, *len(frame).to_bytes(3, "little"), 0x00])  # announce the frame
dev.write(0x02, frame, 3000)
