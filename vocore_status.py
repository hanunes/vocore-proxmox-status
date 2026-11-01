#!/usr/bin/env python3
"""Proxmox status panel for a VoCore USB screen (c872:1004), 800x480, wide.

Safe to install on every node: with no screen attached it only sleeps and
looks again. It reads the Proxmox API and never writes to it.

    vocore_status.py                 run as a service
    vocore_status.py --preview a.png fixture.json [view] [page]    render without USB
"""
import http.server
import json
import pathlib
import ssl
import sys
import threading
import time
import tomllib
import urllib.request

from PIL import Image, ImageDraw, ImageFont

VENDOR, PRODUCT = 0xC872, 0x1004
WIDTH, HEIGHT = 800, 480
ENDPOINT, TOUCH = 0x02, 0x81
TOUCH_REPORT = 14      # bytes, as VoCore's driver reads them
TOUCH_WAKE = 3         # seconds between wakes while waiting for taps
CONFIG = pathlib.Path("/etc/vocore-status/config.toml")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG, CARD, TEXT, DIM = (11, 15, 20), (24, 31, 40), (235, 240, 245), (140, 152, 165)
GOOD, WARN, BAD = (70, 200, 120), (240, 180, 60), (235, 80, 80)
HOSTS_PER_PAGE, SUMMARY_ROWS, STORAGE_ROWS = 2, 5, 8

DEFAULTS = {
    "api_url": "https://127.0.0.1:8006/api2/json",
    "token_id": "vocore@pve!screen",
    "token_file": "/etc/vocore-status/token",
    "interval": 15,        # seconds between data refreshes
    "view": "hosts",       # "summary", "hosts" or "guests"
    "page_seconds": 8,     # seconds on each page when there is more than one
    "poll": 30,            # seconds between looks for an absent screen
    "brightness": 60,      # 0-100
    "flip": False,         # true if the screen is mounted upside down
    "touch": False,        # true: tap left for back, middle to pause, right for next
    "touch_invert": False, # true if left and right tap zones come out swapped
    "pressure_warn": 10,   # % of time stalled: amber from here
    "pressure_bad": 30,    # % of time stalled: red from here
    "swap_warn": 50,       # % of swap used: amber from here
    "backup_warn_hours": 26,
    "replication_warn_minutes": 60,   # the most recent sync of any job
    "replication_max_hours": 26,      # the oldest sync of any job
    "temp_port": 0,        # 0 = off: each node shows only its own temperature
    "nodes": [],           # names in display order; empty means API order
    "hide": [],            # any of HIDEABLE
}


def font(size, bold=False):
    return ImageFont.truetype(BOLD if bold else FONT, size)


# ---- screen ---------------------------------------------------------------

def to_frame(img, flip=False):
    """800x480 RGB image to panel bytes.

    Measured on a 480x800 panel held wide (panel_test.py shows yours):
    the panel takes 800 lines of 480 pixels, line 0 is the left edge and the
    first pixel of a line is at the bottom. Rotating clockwise gives that order.
    """
    if flip:
        img = img.transpose(Image.Transpose.ROTATE_180)
    rotated = img.convert("RGB").transpose(Image.Transpose.ROTATE_270)
    out = bytearray(WIDTH * HEIGHT * 2)
    r, g, b = rotated.split()
    out[1::2] = bytes((rv & 0xF8) | (gv >> 5)
                      for rv, gv in zip(r.tobytes(), g.tobytes()))
    out[0::2] = bytes(((gv & 0x1C) << 3) | (bv >> 3)
                      for gv, bv in zip(g.tobytes(), b.tobytes()))
    return bytes(out)


class Screen:
    def __init__(self, dev, brightness):
        self.dev = dev
        self.last_report = None
        dev.set_configuration()
        self.brightness = brightness
        self.wake()

    def wake(self):
        self._cmd([0x00, 0x29])  # the panel boots asleep
        self._cmd([0x00, 0x51, 0x02, 0, 0, 0, self.brightness, 0])

    def _cmd(self, data):
        self.dev.ctrl_transfer(0x40, 0xB0, 0, 0, data, 100)

    def show(self, frame):
        self._cmd([0x00, 0x2C, *len(frame).to_bytes(3, "little"), 0x00])
        self.dev.write(ENDPOINT, frame, 3000)

    def wait(self, seconds, reverse=False):
        """Sleep, ending early on a tap. Returns tap_action's answer, or None.

        One tap sends a burst, so read until it stops or a held finger would
        act on every report. Touch has gone quiet soon after a wake on a panel
        that was not freshly plugged in (touch_test.py --revive), so keep waking it."""
        import usb.core
        end = time.monotonic() + seconds
        wake_at = 0
        while (left := end - time.monotonic()) > 0:
            if time.monotonic() >= wake_at:
                self.wake()
                wake_at = time.monotonic() + TOUCH_WAKE
            try:
                burst = [bytes(self.dev.read(TOUCH, TOUCH_REPORT,
                                             max(1, int(min(left, 0.25) * 1000))))]
            except usb.core.USBTimeoutError:
                continue
            release = time.monotonic() + 1
            while time.monotonic() < release:
                try:
                    burst.append(bytes(self.dev.read(TOUCH, TOUCH_REPORT, 200)))
                except usb.core.USBTimeoutError:
                    break
            action = tap_action(burst, self.last_report, reverse)
            print(f"tap: {action}, {len(burst)} reports, first {burst[0].hex()}, "
                  f"last {burst[-1].hex()}", file=sys.stderr, flush=True)
            self.last_report = burst[-1]
            return action
        return None


def tap_action(reports, previous, reverse):
    """What a burst of reports asks for: "back", "pause", "next", or "tap" if no
    report has a trustworthy position. Some reports are junk (no sensible touch
    count) or repeat the last touch, which would send a tap the wrong way. A quick
    tap can arrive as a lone release: count 0, state 1, with its position intact."""
    for report in reports:
        if len(report) < 7 or report == previous:
            continue
        if report[2] in (1, 2) or (report[2] == 0 and report[3] >> 6 == 1):
            # the panel's y runs along the long edge, which is left to right held wide
            along = (report[5] & 0x0F) << 8 | report[6]
            along = WIDTH - 1 - along if reverse else along
            return ("back" if along < WIDTH / 3
                    else "pause" if along < WIDTH * 2 / 3 else "next")
    return "tap"


def find_screen():
    import usb.core
    return usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)


# ---- data -----------------------------------------------------------------

class Api:
    def __init__(self, cfg):
        token = pathlib.Path(cfg["token_file"]).read_text().strip()
        self.base = cfg["api_url"].rstrip("/")
        self.auth = f"PVEAPIToken={cfg['token_id']}={token}"
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False        # local node, self-signed
        self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path):
        req = urllib.request.Request(self.base + path,
                                     headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=8, context=self.ctx) as r:
            return json.load(r)["data"]


def attempt(fn, *args):
    """A section that fails shows '?'; it must never stop the display."""
    try:
        return fn(*args)
    except Exception as err:
        print(f"{getattr(fn, '__name__', fn)}{args}: {err}", file=sys.stderr)
        return None


def read_temps(root="/sys/class/hwmon"):
    """Hottest CPU and NVMe sensor in degrees C. Some sensors refuse reads."""
    temps = {}
    for sensor in pathlib.Path(root).glob("hwmon*"):
        try:
            key = {"coretemp": "cpu", "nvme": "nvme"}.get((sensor / "name").read_text().strip())
        except OSError:
            continue
        for reading in sensor.glob("temp*_input") if key else ():
            try:
                value = int(reading.read_text()) / 1000
            except (OSError, ValueError):
                continue
            temps[key] = max(temps.get(key, value), value)
    return temps


class TempHandler(http.server.BaseHTTPRequestHandler):
    """Answers with the temperatures and nothing else, so the screen's node can
    show this node's too. The Proxmox API does not report temperatures."""

    def do_GET(self):
        body = json.dumps(read_temps()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_temps(port):
    server = http.server.ThreadingHTTPServer(("", port), TempHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def remote_temps(ip, port):
    with urllib.request.urlopen(f"http://{ip}:{port}/", timeout=2) as r:
        return json.load(r)


PRESSURE = {"cpu": "pressurecpusome", "mem": "pressurememorysome", "io": "pressureiosome"}
HIDEABLE = ("net", "temp", "pressure", "load", "swap", "disk_io", "tags", "storage_pages")


def network_rate(rows):
    last = [row for row in rows if "netin" in row and "netout" in row][-1]
    return {"in": last["netin"], "out": last["netout"]}


def pressure(rows):
    """Latest stall percentages, or None where the API has none (before PVE 9)."""
    full = [r for r in rows if all(r.get(field) is not None for field in PRESSURE.values())]
    # floats: a guest's status/current gives these as strings, rrddata as numbers
    return {key: float(full[-1][field]) for key, field in PRESSURE.items()} if full else None


def load_and_swap(rows):
    """Latest load average and swap use, or None if the rows have none."""
    fields = ("loadavg", "swapused", "swaptotal")
    full = [r for r in rows if all(r.get(field) is not None for field in fields)]
    return {field: float(full[-1][field]) for field in fields} if full else None


def guest_pressure(api, guest):
    """A guest's pressure isn't in /cluster/resources, so ask for the one on screen:
    one call per page, however many guests there are. None if Proxmox gives none."""
    state = api.get(f"/nodes/{guest['node']}/{guest['type']}/{guest['vmid']}/status/current")
    return pressure([state])


def guest_rates(before, after, seconds):
    """Bytes per second for each guest. The API gives running totals, so compare
    two refreshes rather than asking for each guest's history. A total that went
    down means the guest restarted, so that guest has no rate this time."""
    fields = ("netin", "netout", "diskread", "diskwrite")
    earlier = {r["id"]: r for r in before if "id" in r}
    rates = {}
    for guest in after:
        old = earlier.get(guest.get("id"))
        if old is None or guest.get("type") not in ("qemu", "lxc") or seconds <= 0:
            continue
        change = {f: guest.get(f, 0) - old.get(f, 0) for f in fields}
        if min(change.values()) >= 0:
            rates[guest["id"]] = {f: value / seconds for f, value in change.items()}
    return rates


def collect(api, cfg):
    data = {
        "status": attempt(api.get, "/cluster/status"),
        "resources": attempt(api.get, "/cluster/resources"),
        "tasks": attempt(api.get, "/cluster/tasks"),
        "zfs": {},
        "replication": {},
        "net": {},
        "pressure": {},
        "load": {},
        "temps": {},
    }
    for entry in data["status"] or []:
        if entry.get("type") == "node" and entry.get("online"):
            name = entry["name"]
            data["zfs"][name] = attempt(api.get, f"/nodes/{name}/disks/zfs")
            data["replication"][name] = attempt(api.get, f"/nodes/{name}/replication")
            rows = attempt(api.get, f"/nodes/{name}/rrddata?timeframe=hour")
            data["net"][name] = attempt(network_rate, rows) if rows else None
            data["pressure"][name] = attempt(pressure, rows) if rows else None
            data["load"][name] = attempt(load_and_swap, rows) if rows else None
            if entry.get("local"):
                data["temps"][name] = attempt(read_temps)
            elif cfg["temp_port"] and entry.get("ip"):
                data["temps"][name] = attempt(remote_temps, entry["ip"], cfg["temp_port"])
    return data


# ---- drawing --------------------------------------------------------------

def bytes_gb(value):
    return f"{value / 1024 ** 3:.0f}"


def size(value):
    gb = value / 1024 ** 3
    return f"{gb:.0f}" if gb >= 10 else f"{gb:.1f}"


def fit(d, text, typeface, width):
    """Shorten text that would run into its neighbour; guest names can be long."""
    if d.textlength(text, font=typeface) <= width:
        return text
    while text and d.textlength(text + "…", font=typeface) > width:
        text = text[:-1]
    return text + "…"


def uptime(seconds):
    days, rest = divmod(int(seconds), 86400)
    return f"{days}d {rest // 3600}h" if days else f"{rest // 3600}h {rest % 3600 // 60}m"


def ago(seconds):
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def level(pct):
    return BAD if pct >= 90 else WARN if pct >= 75 else GOOD


def bar(d, x, y, w, pct):
    d.rounded_rectangle((x, y, x + w, y + 10), 5, fill=(45, 54, 66))
    if pct > 0:
        d.rounded_rectangle((x, y, x + max(10, int(w * min(pct, 100) / 100)), y + 10),
                            5, fill=level(pct))


def rate(value):
    return f"{value / 1e6:.1f} MB/s" if value >= 1e5 else f"{value / 1e3:.0f} KB/s"


def is_storage(resource, node):
    return resource.get("type") == "storage" and resource.get("node") == node


def storage_state(pools, stores):
    """Every storage type in one verdict: (words, colour, fullest %), worst first.
    Only ZFS reports health. Other types can only be up or down, and full."""
    used = [p.get("alloc", 0) / max(p.get("size", 1), 1) * 100 for p in pools or []]
    used += [s.get("disk", 0) / s["maxdisk"] * 100 for s in stores
             if s.get("maxdisk") and s.get("status") == "available"]
    fullest = max(used) if used else None
    bad = [p["name"] for p in pools or [] if p.get("health") != "ONLINE"]
    down = [str(s.get("storage", "?")) for s in stores if s.get("status") != "available"]
    if bad:
        return "ZFS PROBLEM: " + ", ".join(bad), BAD, fullest
    if down:
        return "DOWN: " + ", ".join(down), BAD, fullest
    if pools is None:
        return "ZFS ?", WARN, fullest
    return "Storage OK", GOOD, fullest


def pressure_row(stalls, cfg):
    if not stalls:
        return "—", DIM
    worst = max(stalls.values())
    return (f"CPU {stalls['cpu']:.0f}   Mem {stalls['mem']:.0f}   IO {stalls['io']:.0f}",
            BAD if worst >= cfg["pressure_bad"] else WARN if worst >= cfg["pressure_warn"] else TEXT)


def node_card(d, x, y, name, online, res, pools, net, temps, stores, stalls, load, cfg):
    w, h = 384, 240
    d.rounded_rectangle((x, y, x + w, y + h), 12, fill=CARD)
    colour = GOOD if online else BAD if online is False else WARN
    d.ellipse((x + 16, y + 20, x + 32, y + 36), fill=colour)
    d.text((x + 42, y + 12), name, font=font(28, True), fill=TEXT)
    if not res or online is False:
        word = "OFFLINE" if online is False else "?"
        d.text((x + w - 16, y + 18), word, font=font(20, True), fill=colour, anchor="ra")
        return
    d.text((x + w - 16, y + 20), "up " + uptime(res.get("uptime", 0)),
           font=font(18), fill=DIM, anchor="ra")
    cpu = res.get("cpu", 0) * 100
    mem = res.get("mem", 0) / max(res.get("maxmem", 1), 1) * 100
    load_text, swap_text, swap_tone = "", "", DIM
    if load and "load" not in cfg["hide"]:
        load_text = f"load {load['loadavg']:.2f}"
    if load and load["swaptotal"] and "swap" not in cfg["hide"]:
        swap_text = f"swap {size(load['swapused'])}/{size(load['swaptotal'])} GB"
        if load["swapused"] / load["swaptotal"] * 100 >= cfg["swap_warn"]:
            swap_tone = WARN
    for row, (label, pct, note, extra, tone) in enumerate((
            ("CPU", cpu, f"{cpu:.0f}%", load_text, DIM),
            ("RAM", mem, f"{bytes_gb(res.get('mem', 0))}/{bytes_gb(res.get('maxmem', 0))} GB",
             swap_text, swap_tone))):
        top = y + 50 + row * 40
        d.text((x + 16, top), label, font=font(18), fill=DIM)
        right = x + w - 16
        if extra:
            d.text((right, top), extra, font=font(18), fill=tone, anchor="ra")
            right -= d.textlength(extra, font=font(18)) + 16
        d.text((right, top), note, font=font(18), fill=TEXT, anchor="ra")
        bar(d, x + 16, top + 26, w - 32, pct)
    # a hidden row closes up; storage stays at the foot of the card
    top = y + 134
    if "net" not in cfg["hide"]:
        d.text((x + 16, top), "Net", font=font(18), fill=DIM)
        text = f"↓ {rate(net['in'])}   ↑ {rate(net['out'])}" if net else "?"
        d.text((x + w - 16, top), text, font=font(18), fill=TEXT if net else WARN, anchor="ra")
        top += 24
    if "temp" not in cfg["hide"]:
        d.text((x + 16, top), "Temp", font=font(18), fill=DIM)
        if temps:
            parts = [f"{label} {temps[key]:.0f}°"
                     for key, label in (("cpu", "CPU"), ("nvme", "NVMe")) if key in temps]
            d.text((x + w - 16, top), "   ".join(parts), font=font(18),
                   fill=level(max(temps.values())), anchor="ra")
        else:
            d.text((x + w - 16, top), "—", font=font(18), fill=DIM, anchor="ra")
        top += 24
    if "pressure" not in cfg["hide"]:
        d.text((x + 16, top), "Pressure", font=font(18), fill=DIM)
        text, tone = pressure_row(stalls, cfg)
        d.text((x + w - 16, top), text, font=font(18), fill=tone, anchor="ra")
    text, colour, used = storage_state(pools, stores)
    d.text((x + 16, y + 206), fit(d, text, font(18, True), 230), font=font(18, True), fill=colour)
    if used is not None:
        d.text((x + w - 16, y + 206), f"fullest {used:.0f}%", font=font(18),
               fill=level(used), anchor="ra")


def is_guest(resource):
    return resource.get("type") in ("qemu", "lxc") and not resource.get("template")


def guest_tally(d, x, y, name, guests):
    """The partner of a node card: what runs on that node, without the detail."""
    w = 384
    d.rounded_rectangle((x, y, x + w, y + 240), 12, fill=CARD)
    d.text((x + 16, y + 12), "Guests", font=font(28, True), fill=TEXT)
    d.text((x + w - 16, y + 20), "on " + fit(d, name, font(18), 200), font=font(18),
           fill=DIM, anchor="ra")
    running = sum(g.get("status") == "running" for g in guests)
    down = len(guests) - running
    for row, (label, value, colour) in enumerate((
            ("Running", running, GOOD if running else DIM),
            ("Stopped", down, WARN if down else DIM),
            ("VMs", sum(g.get("type") == "qemu" for g in guests), TEXT),
            ("Containers", sum(g.get("type") == "lxc" for g in guests), TEXT))):
        top = y + 62 + row * 42
        d.text((x + 16, top), label, font=font(22), fill=DIM)
        d.text((x + w - 16, top), str(value), font=font(22, True), fill=colour, anchor="ra")


def storage_detail(d, name, stores):
    d.rounded_rectangle((8, 52, 792, 424), 12, fill=CARD)
    d.text((24, 62), "Storage", font=font(28, True), fill=TEXT)
    d.text((776, 70), "on " + fit(d, name, font(18), 300), font=font(18), fill=DIM, anchor="ra")
    for row, store in enumerate(stores):
        top = 104 + row * 39
        up = store.get("status") == "available"
        d.ellipse((24, top + 7, 36, top + 19), fill=GOOD if up else BAD)
        d.text((46, top), fit(d, str(store.get("storage", "?")), font(20, True), 200),
               font=font(20, True), fill=TEXT)
        d.text((256, top + 3), fit(d, str(store.get("plugintype", "?")), font(16), 90),
               font=font(16), fill=DIM)
        if not up:
            d.text((776, top), str(store.get("status", "unknown")).upper(), font=font(20, True),
                   fill=BAD, anchor="ra")
            continue
        disk, maxdisk = store.get("disk", 0), store.get("maxdisk", 0)
        if maxdisk:
            bar(d, 356, top + 8, 270, disk / maxdisk * 100)
        d.text((776, top + 1), f"{size(disk)}/{size(maxdisk)} GB", font=font(18),
               fill=TEXT, anchor="ra")


def guest_detail(d, guest, rates, stalls, cfg):
    x, y, w = 8, 52, 784
    d.rounded_rectangle((x, y, x + w, y + 372), 12, fill=CARD)
    state = guest.get("status", "unknown")
    running = state == "running"
    d.ellipse((x + 16, y + 22, x + 34, y + 40), fill=GOOD if running else WARN)
    d.text((x + 46, y + 10), fit(d, str(guest.get("name", "?")), font(34, True), 470),
           font=font(34, True), fill=TEXT)
    kind = "VM" if guest.get("type") == "qemu" else "CT"
    d.text((x + w - 16, y + 12), f"{kind} {guest.get('vmid', '?')} on {guest.get('node', '?')}",
           font=font(18), fill=DIM, anchor="ra")
    word = f"up {uptime(guest.get('uptime', 0))}" if running else state.upper()
    d.text((x + w - 16, y + 36), word, font=font(18, not running),
           fill=DIM if running else WARN, anchor="ra")

    cores = guest.get("maxcpu", 0)
    cpu = guest.get("cpu", 0) * 100
    mem, maxmem = guest.get("mem", 0), guest.get("maxmem", 0)
    disk, maxdisk = guest.get("disk", 0), guest.get("maxdisk", 0)
    cpu_note = f"{cpu:.0f}% of {cores} " + ("core" if cores == 1 else "cores") if cores else f"{cpu:.0f}%"
    rows = []
    if running:
        rows.append(("CPU", cpu, cpu_note))
        rows.append(("RAM", mem / max(maxmem, 1) * 100, f"{size(mem)}/{size(maxmem)} GB"))
        # VMs report no disk use without help from inside the guest, only the size
        if disk:
            rows.append(("Disk", disk / max(maxdisk, 1) * 100, f"{size(disk)}/{size(maxdisk)} GB"))
    top = y + 72
    for label, pct, note in rows:
        d.text((x + 16, top), label, font=font(20), fill=DIM)
        d.text((x + w - 16, top), note, font=font(20), fill=TEXT, anchor="ra")
        bar(d, x + 16, top + 28, w - 32, pct)
        top += 52

    pairs = []
    if not running:
        pairs.append(("RAM", f"{size(maxmem)} GB assigned", TEXT))
    if not disk:
        pairs.append(("Disk", f"{size(maxdisk)} GB assigned" if maxdisk else "—",
                      TEXT if maxdisk else DIM))
    if running and "net" not in cfg["hide"]:
        pairs.append(("Net", f"↓ {rate(rates['netin'])}   ↑ {rate(rates['netout'])}"
                      if rates else "—", TEXT if rates else DIM))
    if running and "disk_io" not in cfg["hide"]:
        pairs.append(("Disk I/O", f"R {rate(rates['diskread'])}   W {rate(rates['diskwrite'])}"
                      if rates else "—", TEXT if rates else DIM))
    if running and stalls and "pressure" not in cfg["hide"]:
        pairs.append(("Pressure", *pressure_row(stalls, cfg)))
    tags = str(guest.get("tags") or "").replace(";", ", ")
    if tags and "tags" not in cfg["hide"]:
        pairs.append(("Tags", fit(d, tags, font(20), 640), DIM))
    for label, text, colour in pairs:
        d.text((x + 16, top), label, font=font(20), fill=DIM)
        d.text((x + w - 16, top), text, font=font(20), fill=colour, anchor="ra")
        top += 34


def summary_row(d, y, name, online, res, pools, guests, stores):
    d.rounded_rectangle((8, y, 792, y + 42), 10, fill=CARD)
    colour = GOOD if online else BAD if online is False else WARN
    d.ellipse((20, y + 14, 34, y + 28), fill=colour)
    d.text((44, y + 21), fit(d, name, font(22, True), 136), font=font(22, True),
           fill=TEXT, anchor="lm")
    if not res or online is False:
        d.text((780, y + 21), "OFFLINE" if online is False else "?", font=font(20, True),
               fill=colour, anchor="rm")
        return
    cpu = res.get("cpu", 0) * 100
    mem = res.get("mem", 0) / max(res.get("maxmem", 1), 1) * 100
    for x, label, pct in ((190, "CPU", cpu), (360, "RAM", mem)):
        d.text((x, y + 21), f"{label} {pct:.0f}%", font=font(16), fill=DIM, anchor="lm")
        bar(d, x + 88, y + 16, 62, pct)
    running = sum(g.get("status") == "running" for g in guests)
    d.text((530, y + 21), f"{running}/{len(guests)} up", font=font(18),
           fill=TEXT if running == len(guests) else WARN, anchor="lm")
    text, tone, _ = storage_state(pools, stores)
    d.text((780, y + 21), fit(d, text, font(18, True), 170), font=font(18, True),
           fill=tone, anchor="rm")


def line(d, y, label, text, colour):
    d.text((24, y), label, font=font(22), fill=DIM)
    d.text((776, y), text, font=font(22, True), fill=colour, anchor="ra")


def summary_guests(resources):
    if resources is None:
        return "?", WARN
    guests = [r for r in resources if is_guest(r)]
    down = sum(r.get("status") != "running" for r in guests)
    text = f"{len(guests) - down} running"
    if down:
        text += f", {down} stopped"
    return text, WARN if down else GOOD


def summary_quorum(status):
    if status is None:
        return "?", WARN
    cluster = next((e for e in status if e.get("type") == "cluster"), None)
    if cluster is None:
        return ("standalone", DIM) if status else ("?", WARN)
    return ("quorate", GOOD) if cluster.get("quorate") else ("NO QUORUM", BAD)


def summary_replication(replication, now, cfg):
    if not replication or any(v is None for v in replication.values()):
        return "?", WARN
    jobs = [job for jobs in replication.values() for job in jobs if not job.get("disable")]
    if not jobs:
        return "no jobs", DIM
    if any(j.get("fail_count") or j.get("error") for j in jobs):
        return "FAILING", BAD
    ages = [now - j.get("last_sync", 0) for j in jobs]
    noun = "job" if len(jobs) == 1 else "jobs"
    text = f"{len(jobs)} {noun}, latest {ago(min(ages))}"
    # The frequent jobs prove replication is running; the daily ones would look
    # late against that limit, so they only have to beat the slower one.
    late = (min(ages) > cfg["replication_warn_minutes"] * 60
            or max(ages) > cfg["replication_max_hours"] * 3600)
    return text, WARN if late else GOOD


def summary_backup(tasks, now, cfg):
    if tasks is None:
        return "?", WARN
    runs = [t for t in tasks if t.get("type") == "vzdump" and t.get("endtime")]
    if not runs:
        return "none in task log", WARN
    # Each node runs its own backup, so judge the latest run of each: one node's
    # good run must not hide a failure on the other.
    latest = {}
    for run in sorted(runs, key=lambda t: t["endtime"]):
        latest[run.get("node")] = run
    statuses = {node: str(run.get("status")) for node, run in latest.items()}
    warned = sorted(n for n, s in statuses.items() if s.startswith("WARNINGS"))
    failed = sorted(n for n, s in statuses.items() if s != "OK" and n not in warned)
    missing = [n for n in cfg["nodes"] if n not in latest]
    if failed:
        return "FAILED on " + ", ".join(failed), BAD
    if missing:
        return "none listed for " + ", ".join(missing), WARN
    if warned:
        return "warnings on " + ", ".join(warned), WARN
    age = now - min(run["endtime"] for run in latest.values())
    return ago(age), WARN if age > cfg["backup_warn_hours"] * 3600 else GOOD


def view_pages(data, cfg):
    """What the configured view shows, as a list of (kind, subject) pages."""
    names = cfg["nodes"] or sorted(e["name"] for e in data.get("status") or []
                                   if e.get("type") == "node")
    if cfg["view"] == "summary":
        return [("summary", names[i:i + SUMMARY_ROWS])
                for i in range(0, len(names), SUMMARY_ROWS)]
    if cfg["view"] == "guests":
        guests = [r for r in data.get("resources") or [] if is_guest(r)]
        stores = [r for r in data.get("resources") or [] if r.get("type") == "storage"]
        pages = []
        for name in names:
            pages.append(("host", name))
            here = sorted((s for s in stores if s.get("node") == name),
                          key=lambda s: (s.get("status") == "available", str(s.get("storage"))))
            if "storage_pages" not in cfg["hide"]:
                pages += [("storage", (name, here[i:i + STORAGE_ROWS]))
                          for i in range(0, len(here), STORAGE_ROWS)]
            pages += [("guest", g) for g in sorted(
                (g for g in guests if g.get("node") == name), key=lambda g: g.get("vmid", 0))]
        return pages
    return [("hosts", names[i:i + HOSTS_PER_PAGE])
            for i in range(0, len(names), HOSTS_PER_PAGE)]


def cluster_lines(data, now, cfg):
    return (("Cluster", summary_quorum(data.get("status"))),
            ("Guests", summary_guests(data.get("resources"))),
            ("Replication", summary_replication(data.get("replication"), now, cfg)),
            ("Last backup", summary_backup(data.get("tasks"), now, cfg)))


def draw_cluster_lines(d, data, now, cfg):
    for row, (label, (text, colour)) in enumerate(cluster_lines(data, now, cfg)):
        line(d, 302 + row * 43, label, text, colour)


def alert_footer(d, data, now, cfg):
    """A guest page has no room for the cluster lines, but a failed backup must
    not wait for the rotation to come round. Stopped guests are left out: many
    are stopped on purpose, and this line would never go green."""
    rows = [(label, text, colour) for label, (text, colour) in cluster_lines(data, now, cfg)
            if label != "Guests"]
    alerts = sorted((r for r in rows if r[2] in (BAD, WARN)), key=lambda r: r[2] != BAD)
    if not alerts:
        d.text((400, 452), "Cluster, replication and backups OK", font=font(20, True),
               fill=GOOD, anchor="mm")
        return
    label, text, colour = alerts[0]
    more = f"   +{len(alerts) - 1} more" if len(alerts) > 1 else ""
    d.text((400, 452), fit(d, f"{label}: {text}{more}", font(20, True), 770),
           font=font(20, True), fill=colour, anchor="mm")


def draw_node(d, slot, name, data, online, cards, cfg):
    node_card(d, 8 + slot * 400, 52, name, online.get(name), cards.get(name),
              data.get("zfs", {}).get(name), data.get("net", {}).get(name),
              data.get("temps", {}).get(name),
              [r for r in data.get("resources") or [] if is_storage(r, name)],
              data.get("pressure", {}).get(name), data.get("load", {}).get(name), cfg)


def render(data, cfg, now, index=0, paused=False):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    d = ImageDraw.Draw(img)
    d.text((16, 8), "Proxmox", font=font(26, True), fill=TEXT)
    # the clock is the freeze detector: a stopped clock means stale data
    d.text((784, 6), time.strftime("%H:%M:%S", time.localtime(now)),
           font=font(30, True), fill=TEXT, anchor="ra")

    pages = view_pages(data, cfg)
    if not pages:
        d.text((400, 240), "API unavailable", font=font(34, True), fill=BAD, anchor="mm")
        return img
    index %= len(pages)
    marker = f"{index + 1}/{len(pages)}" if len(pages) > 1 else ""
    if paused:
        marker = f"{marker}  PAUSED".strip()
    if marker:
        d.text((400, 22), marker, font=font(18, paused), fill=WARN if paused else DIM, anchor="mm")

    status = data.get("status") or []
    resources = data.get("resources") or []
    online = {e["name"]: bool(e.get("online")) for e in status if e.get("type") == "node"}
    cards = {r.get("node"): r for r in resources if r.get("type") == "node"}
    kind, subject = pages[index]
    if kind == "hosts":
        for slot, name in enumerate(subject):
            draw_node(d, slot, name, data, online, cards, cfg)
        draw_cluster_lines(d, data, now, cfg)
    elif kind == "host":
        draw_node(d, 0, subject, data, online, cards, cfg)
        guest_tally(d, 408, 52, subject,
                    [r for r in resources if is_guest(r) and r.get("node") == subject])
        draw_cluster_lines(d, data, now, cfg)
    elif kind == "storage":
        storage_detail(d, *subject)
        alert_footer(d, data, now, cfg)
    elif kind == "guest":
        guest_detail(d, subject, data.get("guest_rates", {}).get(subject.get("id")),
                     data.get("guest_pressure", {}).get(subject.get("id")), cfg)
        alert_footer(d, data, now, cfg)
    else:
        for row, name in enumerate(subject):
            summary_row(d, 52 + row * 48, name, online.get(name), cards.get(name),
                        data.get("zfs", {}).get(name),
                        [r for r in resources if is_guest(r) and r.get("node") == name],
                        [r for r in resources if is_storage(r, name)])
        draw_cluster_lines(d, data, now, cfg)
    return img


# ---- main -----------------------------------------------------------------

def run(cfg):
    import usb.core
    api = screen = seen = None
    data, seen_at, refresh_at, index, paused = {}, 0, 0, 0, False
    reverse = cfg["flip"] != cfg["touch_invert"]
    print(f"starting: view {cfg['view']}, touch {'on' if cfg['touch'] else 'off'}",
          file=sys.stderr, flush=True)
    while True:
        if screen is None:
            dev = attempt(find_screen)
            if dev is None:
                time.sleep(cfg["poll"])
                continue
            screen = attempt(Screen, dev, cfg["brightness"])
            if screen is None:
                time.sleep(cfg["poll"])
                continue
        # One loop for fetching and paging: if a fetch hangs, the clock stops too.
        if time.time() >= refresh_at:
            api = api or attempt(Api, cfg)
            data = collect(api, cfg) if api else {}
            fresh, fresh_at = data.get("resources"), time.time()
            if seen and fresh:
                data["guest_rates"] = guest_rates(seen, fresh, fresh_at - seen_at)
            seen, seen_at = fresh, fresh_at
            refresh_at = fresh_at + cfg["interval"]
        pages = view_pages(data, cfg)
        kind, subject = pages[index % len(pages)] if pages else (None, None)
        if (api and kind == "guest" and subject.get("status") == "running"
                and "pressure" not in cfg["hide"]):
            data.setdefault("guest_pressure", {})[subject.get("id")] = \
                attempt(guest_pressure, api, subject)
        frame = to_frame(render(data, cfg, time.time(), index, paused), cfg["flip"])
        pause = cfg["page_seconds"] if len(pages) > 1 and not paused else cfg["interval"]
        tap = None
        try:
            screen.show(frame)
            if cfg["touch"]:
                tap = screen.wait(pause, reverse)
        except usb.core.USBError as err:   # unplugged: go back to looking
            print(f"screen lost: {err}", file=sys.stderr)
            screen = None
            continue
        if not cfg["touch"]:
            time.sleep(pause)
        if tap == "pause":
            paused = not paused
        elif tap == "back":
            index -= 1
        elif tap == "tap" and paused:       # no position: frozen touch must not trap a pause
            paused = False
        elif tap == "next" or not paused:   # includes a tap with no usable position
            index += 1


def main():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(tomllib.loads(CONFIG.read_text()))
    if cfg["view"] not in ("summary", "hosts", "guests"):
        print(f"unknown view {cfg['view']!r}, using hosts", file=sys.stderr)
        cfg["view"] = "hosts"
    for item in cfg["hide"]:
        if item not in HIDEABLE:
            print(f"unknown hide item {item!r}; choose from {', '.join(HIDEABLE)}", file=sys.stderr)
    if 4 <= len(sys.argv) <= 6 and sys.argv[1] == "--preview":
        fixture = json.loads(pathlib.Path(sys.argv[3]).read_text())
        if len(sys.argv) > 4:
            cfg["view"] = sys.argv[4]
        page = int(sys.argv[5]) - 1 if len(sys.argv) > 5 else 0
        render(fixture, cfg, fixture["now"], page).save(sys.argv[2])
        return
    if sys.argv[1:] == ["--dump"]:   # the raw API data, for checking field names
        print(json.dumps({**collect(Api(cfg), cfg), "now": int(time.time())}, indent=1))
        return
    if cfg["temp_port"]:
        attempt(serve_temps, cfg["temp_port"])
    run(cfg)


if __name__ == "__main__":
    main()
