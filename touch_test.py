#!/usr/bin/env python3
"""Find out whether a VoCore screen reports touch. Needs python3-usb; run as root.

    systemctl stop vocore-status
    python3 touch_test.py          follow the prompts
    python3 touch_test.py --flip   for a screen mounted upside down
    python3 touch_test.py --watch  print anything the screen sends for 60 seconds
    python3 touch_test.py --frames check whether sending frames stops touch reports:
                                   no keyboard needed, tap while the screen is white
    python3 touch_test.py --thaw   run while taps repeat one old report: can software clear it?
                                   no keyboard needed; two squares mean it has finished
    python3 touch_test.py --revive when does touch stop, and what brings it back?
                                   no keyboard needed; two squares mean it has finished
    systemctl start vocore-status

White targets appear on the screen. Each step records what the touch endpoint
sends, and the raw reports are saved to touch_capture.txt for working out the
report format. Hold the screen as panel_test.py describes: red block on the left.
"""
import pathlib
import shutil
import subprocess
import sys
import threading
import time

import usb.core
import usb.util

VENDOR, PRODUCT = 0xC872, 0x1004
WIDTH, HEIGHT = 800, 480
FRAMES, TOUCH = 0x02, 0x81
CAPTURE = "touch_capture.txt"
IDLE = "no touch"
REPORT = 14          # the size VoCore's own driver (mpro.c) reads
WATCH_SECONDS = 60
PHASE_SECONDS, FRAME_EVERY = 16, 4
LEAD_SECONDS, GAP_SECONDS = 30, 6   # to reach the screen; between steps
FRAMES_LOG = "touch_frames.txt"
REVIVE_LOG = "touch_revive.txt"
THAW_LOG = "touch_thaw.txt"
THAW_LEAD, THAW_TRY = 20, 12   # seconds
REVIVE_LEAD, REVIVE_WATCH, REVIVE_TRY, REVIVE_REST = 20, 40, 12, 45   # seconds
# label, send frames, keep a read waiting while frames go out (as VoCore's driver does)
PHASES = (("no frames", False, False),
          ("frames, reader always waiting", True, True),
          ("frames between reads, as the service does", True, False))

# label, what to do, targets as (left, top, right, bottom), seconds allowed
STEPS = (
    (IDLE, "Keep your hands off the screen", (), 4),
    ("top left", "Tap the target", ((20, 20, 140, 140),), 10),
    ("top right", "Tap the target", ((660, 20, 780, 140),), 10),
    ("bottom left", "Tap the target", ((20, 340, 140, 460),), 10),
    ("bottom right", "Tap the target", ((660, 340, 780, 460),), 10),
    ("drag", "Drag slowly along the bar, left to right", ((20, 220, 780, 260),), 10),
    ("two fingers", "Touch both targets at once",
     ((140, 180, 260, 300), (540, 180, 660, 300)), 10),
)


def frame(boxes, flip=False):
    """White boxes on black, in panel order (see to_frame in vocore_status.py):
    800 lines of 480 pixels, line 0 on the left, each line starting at the bottom."""
    out = bytearray(WIDTH * HEIGHT * 2)
    for left, top, right, bottom in boxes:
        if flip:
            left, top, right, bottom = WIDTH - right, HEIGHT - bottom, WIDTH - left, HEIGHT - top
        white = b"\xFF\xFF" * (bottom - top)
        for x in range(left, right):
            start = (x * HEIGHT + HEIGHT - bottom) * 2
            out[start:start + len(white)] = white
    return bytes(out)


def capture(read, seconds, quiet=1.0):
    """Reports until the time is up, or until `quiet` seconds after the last one."""
    reports = []
    last = None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            reports.append(bytes(read()))
            last = time.monotonic()
        except usb.core.USBTimeoutError:
            if last is not None and time.monotonic() - last > quiet:
                break
    return reports


def changing_bytes(reports):
    """Byte positions that differ between reports: likely flags and coordinates."""
    if not reports:
        return []
    size = min(len(r) for r in reports)
    return [i for i in range(size) if len({r[i] for r in reports}) > 1]


def verdict(results):
    """(touch clearly works, what to tell the person)."""
    touched = {label: reports for label, reports in results.items() if label != IDLE}
    silent = [label for label, reports in touched.items() if not reports]
    if results.get(IDLE):
        return False, ("UNCLEAR: reports arrived with nothing touching the screen. "
                       f"Check {CAPTURE} before trusting the other steps.")
    if len(silent) == len(touched):
        return False, ("NO REPORTS: this unit may have no touch layer, or it may need a "
                       "setup step nobody has documented. This does not prove there is no touch.")
    if silent:
        return False, "PARTIAL: touch responded, but not for: " + ", ".join(silent)
    return True, "TOUCH WORKS: every step sent reports."


def decode(report):
    """First touch point, in the panel's own 480x800 coordinates. The layout is from
    VoCore's mpro.c and matches a real screen. Some reports repeat old or junk data
    (count 255 was seen), so the raw bytes are always shown beside this."""
    if len(report) < 7:
        return "too short to decode"
    if report[2] > 2:
        return f"junk (count {report[2]})"
    x = (report[3] & 0x0F) << 8 | report[4]
    y = (report[5] & 0x0F) << 8 | report[6]
    state = {0: "down", 1: "up", 2: "held"}.get(report[3] >> 6, "?")
    return f"count {report[2]} {state} point {report[5] >> 4} x {x} y {y}"


def watch(read, seconds):
    """Print reports as they arrive. Returns how many came."""
    count = 0
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        try:
            report = bytes(read())
        except usb.core.USBTimeoutError:
            continue
        count += 1
        print(f"{time.monotonic() - start:6.2f}s  {report.hex()}  {decode(report)}", flush=True)
    return count


def run_phase(read, send, frames, background, seconds=PHASE_SECONDS, every=FRAME_EVERY):
    """Read for a while, sending frames if asked. Returns reports per `every` seconds:
    [12, 0, 0, 0] would mean touch stopped once a second frame went out."""
    start = time.monotonic()
    times = []
    failed = []
    stop = threading.Event()

    def read_once():
        try:
            read()
            times.append(time.monotonic() - start)
        except usb.core.USBTimeoutError:
            pass

    def reader():
        try:
            while not stop.is_set():
                read_once()
        except usb.core.USBError as err:
            failed.append(err)

    thread = threading.Thread(target=reader) if background else None
    if thread:
        thread.start()
    due = 0
    try:
        while (now := time.monotonic() - start) < seconds and not failed:
            if frames and now >= due:
                send()
                due += every
            if thread:
                time.sleep(0.02)
            else:
                read_once()
    finally:
        stop.set()
        if thread:
            thread.join()
    if failed:
        raise failed[0]
    bins = [0] * (seconds // every + 1)
    for moment in times:
        bins[int(moment // every)] += 1
    return bins[:seconds // every]


def authorized_file(dev):
    """The sysfs switch that disconnects this device without unplugging it."""
    for folder in pathlib.Path("/sys/bus/usb/devices").glob("*"):
        try:
            if (int((folder / "busnum").read_text()) == dev.bus
                    and int((folder / "devnum").read_text()) == dev.address):
                return folder / "authorized"
        except (OSError, ValueError):
            continue
    raise OSError("the screen is not in /sys/bus/usb/devices")


def service_running():
    if not shutil.which("systemctl"):
        return False
    state = subprocess.run(["systemctl", "is-active", "--quiet", "vocore-status"], check=False)
    return state.returncode == 0


def main():
    args = sys.argv[1:]
    if args not in ([], ["--flip"], ["--watch"], ["--frames"], ["--revive"], ["--thaw"]):
        sys.exit(__doc__)
    if service_running():
        sys.exit("vocore-status is using the screen. Run: systemctl stop vocore-status")
    dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
    if dev is None:
        sys.exit("No VoCore screen found (USB ID c872:1004). Check lsusb and the cable.")
    dev.set_configuration()
    endpoint = usb.util.find_descriptor(dev.get_active_configuration()[(0, 0)],
                                        bEndpointAddress=TOUCH)
    if endpoint is None:
        sys.exit("This screen has no touch endpoint (0x81).")
    print(f"Endpoint 0x81: {endpoint.wMaxPacketSize} byte packets, "
          f"interval {endpoint.bInterval}")

    cmd = lambda data: dev.ctrl_transfer(0x40, 0xB0, 0, 0, data, 100)
    cmd([0x00, 0x29])                       # wake: the panel boots asleep
    cmd([0x00, 0x51, 0x02, 0, 0, 0, 50, 0])  # brightness

    def show(boxes):
        data = frame(boxes, args == ["--flip"])
        cmd([0x00, 0x2C, *len(data).to_bytes(3, "little"), 0x00])
        dev.write(FRAMES, data, 3000)

    if args == ["--watch"]:
        show(((0, 0, WIDTH, HEIGHT),))
        print(f"The screen is white. Touch it anywhere for {WATCH_SECONDS} seconds, "
              "with a firm press and a slow drag.", flush=True)
        try:
            count = watch(lambda: dev.read(TOUCH, REPORT, 250), WATCH_SECONDS)
        except usb.core.USBError as err:
            sys.exit(f"Reading the touch endpoint failed: {err}")
        show(())
        print(f"\n{count} reports\nRestart the panel: systemctl start vocore-status")
        sys.exit(0 if count else 1)

    read = lambda: dev.read(TOUCH, REPORT, 250)
    if args == ["--frames"]:
        white = ((0, 0, WIDTH, HEIGHT),)
        show(())
        total = LEAD_SECONDS + len(PHASES) * PHASE_SECONDS + (len(PHASES) - 1) * GAP_SECONDS
        print(f"Starts in {LEAD_SECONDS} seconds and takes about {total} seconds in all.\n"
              f"Tap about once a second while the screen is white, and stop while it is black.\n"
              f"It turns white {len(PHASES)} times. Results also go to {FRAMES_LOG}.", flush=True)
        time.sleep(LEAD_SECONDS)
        with open(FRAMES_LOG, "w", encoding="ascii") as log:
            for number, (label, frames, background) in enumerate(PHASES, 1):
                show(white)
                bins = run_phase(read, lambda: show(white), frames, background)
                show(())
                line = f"{number}/{len(PHASES)} {label}: {bins}"
                print(line, flush=True)
                log.write(line + "\n")
                log.flush()
                if number < len(PHASES):
                    time.sleep(GAP_SECONDS)
        print("\nSend these numbers. Restart the panel: systemctl start vocore-status")
        sys.exit(0)

    white = ((0, 0, WIDTH, HEIGHT),)
    finished = ((100, 140, 300, 340), (500, 140, 700, 340))

    def wake():
        cmd([0x00, 0x29])
        cmd([0x00, 0x51, 0x02, 0, 0, 0, 50, 0])

    if args == ["--thaw"]:
        def panel_off_on():
            cmd([0x00, 0x28])      # display off, the partner of the wake command
            time.sleep(3)
            wake()

        def disconnect():
            nonlocal dev
            switch = authorized_file(dev)
            switch.write_text("0")
            time.sleep(3)
            switch.write_text("1")
            time.sleep(3)
            dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
            dev.set_configuration()
            wake()

        show(())
        print(f"Tap different places about once a second whenever the screen is white. First "
              f"white in {THAW_LEAD} seconds.\nTwo white squares mean it has finished, within "
              f"about 2 minutes. Results also go to {THAW_LOG}.", flush=True)
        time.sleep(THAW_LEAD)
        with open(THAW_LOG, "w", encoding="ascii") as log:
            for label, action in (("nothing, to see the frozen state", None),
                                  ("display off and on", panel_off_on),
                                  ("USB disconnect", disconnect)):
                show(())
                try:
                    if action:
                        action()
                    show(white)
                    reports = capture(lambda: dev.read(TOUCH, REPORT, 250), THAW_TRY, THAW_TRY)
                except (usb.core.USBError, OSError, AttributeError) as err:
                    line = f"{label}: failed: {err}"
                    reports = []
                else:
                    line = (f"{label}: {len(reports)} reports, {len(set(reports))} different, "
                            f"last {reports[-1].hex() if reports else 'none'}")
                print(line, flush=True)
                log.write(line + "\n")
                log.flush()
                if action and len(set(reports)) > 1:
                    break
        try:
            show(finished)
        except (usb.core.USBError, AttributeError):
            pass
        print("\nSend these lines. Restart the panel: systemctl start vocore-status")
        sys.exit(0)

    if args == ["--revive"]:
        def configure():
            dev.set_configuration()
            wake()

        def reset():
            nonlocal dev
            dev.reset()
            time.sleep(3)      # it may come back as a new device
            dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
            configure()

        started = time.monotonic()
        show(())
        print(f"Tap about once a second whenever the screen is white. First white in "
              f"{REVIVE_LEAD} seconds.\nTwo white squares mean it has finished, within about "
              f"3 minutes. Results also go to {REVIVE_LOG}.", flush=True)
        time.sleep(REVIVE_LEAD)
        with open(REVIVE_LOG, "w", encoding="ascii") as log:
            def note(line):
                print(line, flush=True)
                log.write(line + "\n")
                log.flush()

            def attempt(label, action):
                """Black, try the action, then a white window. True if reports came."""
                show(())
                time.sleep(GAP_SECONDS)
                try:
                    action()
                    show(white)
                    bins = run_phase(read, None, False, False, REVIVE_TRY)
                except usb.core.USBError as err:
                    note(f"{label}: failed: {err}")
                    return False
                note(f"{label}: {bins}")
                return sum(bins) > 0

            show(white)
            note(f"reports per {FRAME_EVERY}s, from {time.monotonic() - started:.0f}s after setup: "
                 f"{run_phase(read, None, False, False, REVIVE_WATCH)}")
            for label, action in (("wake command", wake), ("set configuration", configure),
                                  ("usb reset", reset)):
                if attempt(label, action):
                    show(())
                    time.sleep(REVIVE_REST)
                    attempt(f"{label}, second time", action)
                    break
        show(finished)
        print("\nSend these lines. Restart the panel: systemctl start vocore-status")
        sys.exit(0)

    results = {}
    with open(CAPTURE, "w", encoding="ascii") as log:
        for number, (label, task, boxes, seconds) in enumerate(STEPS, 1):
            show(boxes)
            when = "wait" if label == IDLE else "do it"
            input(f"\n{number}/{len(STEPS)} {label}: {task}.\n"
                  f"    Recording starts when you press Enter. Press Enter, then {when} "
                  f"(you have {seconds} seconds): ")
            print("    recording...", flush=True)
            quiet = seconds if label == IDLE else 1.0
            results[label] = capture(read, seconds, quiet)
            for report in results[label]:
                log.write(f"{label}\t{report.hex()}\n")
            log.flush()
            print(f"    {len(results[label])} reports")
            if results[label]:
                print("    first", results[label][0].hex())
                print("    last ", results[label][-1].hex())
    show(())

    every = [r for label, reports in results.items() if label != IDLE for r in reports]
    if every:
        print("\nBytes that changed:", changing_bytes(every))
        print("Report sizes:", sorted({len(r) for r in every}))
    worked, message = verdict(results)
    print(f"\n{message}\nRaw reports: {CAPTURE}\nRestart the panel: systemctl start vocore-status")
    sys.exit(0 if worked else 1)


if __name__ == "__main__":
    main()
