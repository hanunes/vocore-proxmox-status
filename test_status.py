"""Offline checks: pixel mapping against the panel test (see panel_test.py), and absent/failed paths."""
import json
import time
import pathlib
import sys
from unittest import mock

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import vocore_status as v

# 1. panel_test.py shows red on the left and green at the bottom. Drawing that
#    picture must produce exactly the bytes it sends.
RED, GREEN, BLUE = b"\x00\xF8", b"\xE0\x07", b"\x1F\x00"
sent = b"".join(GREEN * 40 + (RED if y < 100 else BLUE) * 440 for y in range(800))
img = Image.new("RGB", (800, 480), (0, 0, 255))
d = ImageDraw.Draw(img)
d.rectangle((0, 0, 99, 479), fill=(255, 0, 0))
d.rectangle((0, 440, 799, 479), fill=(0, 255, 0))
start = time.time()
frame = v.to_frame(img)
print("convert seconds:", round(time.time() - start, 3))
assert len(frame) == 768000
assert frame == sent, "mapping differs from the panel test"
print("mapping OK")

cfg = dict(v.DEFAULTS)
fixture = json.loads((HERE / "fixture.json").read_text())

# 2. Every data gap renders instead of crashing.
for gap in ({}, {"status": None, "resources": None, "tasks": None},
            {**fixture, "zfs": {}}, {**fixture, "resources": None},
            {**fixture, "tasks": []}, {**fixture, "replication": {"pve1": None}}):
    v.to_frame(v.render(gap, cfg, fixture["now"]))
print("gaps OK")

# 3. No screen: the loop only sleeps and never touches the API or token.
class Stop(Exception):
    pass

with mock.patch.object(v, "find_screen", return_value=None), \
     mock.patch.object(v, "Api", side_effect=AssertionError("API used")), \
     mock.patch.object(v.time, "sleep", side_effect=Stop) as sleep:
    try:
        v.run(cfg)
    except Stop:
        pass
assert sleep.call_args.args == (cfg["poll"],)
print("no screen OK")

# 4. Unplugged mid-run: back to looking, and the panel is woken again on return.
import usb.core
dev = mock.MagicMock()
dev.write.side_effect = usb.core.USBError("gone")
steps = [dev, None]
with mock.patch.object(v, "find_screen", side_effect=lambda: steps.pop(0)), \
     mock.patch.object(v, "Api", side_effect=OSError("no token")), \
     mock.patch.object(v.time, "sleep", side_effect=Stop):
    try:
        v.run(cfg)
    except Stop:
        pass
assert dev.write.called and not steps
print("unplug OK")

# 5. Temperatures: the sensor layout from pve1, including a sensor that refuses reads.
import tempfile
root = pathlib.Path(tempfile.mkdtemp())
for i, (name, files) in enumerate((("acpitz", {"temp1_input": "27800"}),
                                   ("nvme", {"temp1_input": "35850", "temp2_input": "52850"}),
                                   ("coretemp", {"temp1_input": "55000", "temp7_input": "58000"}),
                                   ("iwlwifi_1", {"temp1_input": ""}))):
    folder = root / f"hwmon{i}"
    folder.mkdir()
    (folder / "name").write_text(name + "\n")
    for file, value in files.items():
        (folder / file).write_text(value)
(root / "hwmon4").mkdir()   # a sensor with no name file
(root / "hwmon2" / "temp9_input").mkdir()   # stands in for "No data available"
temps = v.read_temps(root)
assert temps == {"cpu": 58.0, "nvme": 52.85}, temps
print("temps OK")

# 6. The temperature port answers over real HTTP, and a dead node does not stall.
v.read_temps, real = (lambda: {"cpu": 54.0}), v.read_temps
v.serve_temps(18765)
assert v.remote_temps("127.0.0.1", 18765) == {"cpu": 54.0}
v.read_temps = real
print("port OK")

# 7. Collecting: rates from the last full row; remote temps stay off unless asked.
class FakeApi:
    def get(self, path):
        if path == "/cluster/status":
            return [{"type": "node", "name": "pve1", "online": 1, "local": 1, "ip": "127.0.0.1"},
                    {"type": "node", "name": "pve2", "online": 1, "local": 0, "ip": "127.0.0.1"}]
        if "rrddata" in path:
            return [{"netin": 1, "netout": 2}, {"netin": 1612136.4, "netout": 79530.6}, {"time": 9}]
        return []

off = v.collect(FakeApi(), cfg)
assert off["net"]["pve1"] == {"in": 1612136.4, "out": 79530.6}
assert "pve2" not in off["temps"], off["temps"]
assert v.rate(1612136.4) == "1.6 MB/s" and v.rate(79530.6) == "80 KB/s"
print("collect OK")

# 8. Views: paging, including more nodes than one page holds.
def nodes(count):
    return [{"type": "node", "name": f"pve{i}", "online": 1} for i in range(count)]

def kinds(data, view):
    return [kind for kind, _ in v.view_pages(data, {**cfg, "view": view})]

assert kinds(fixture, "hosts") == ["hosts"]
assert kinds(fixture, "summary") == ["summary"]
assert kinds(fixture, "guests") == ["host", "storage", "guest", "guest"] * 2
assert kinds({"status": nodes(7)}, "hosts") == ["hosts"] * 4
assert kinds({"status": nodes(7)}, "summary") == ["summary"] * 2
assert v.view_pages({}, {**cfg, "view": "guests"}) == []
shown = [g["vmid"] for kind, g in v.view_pages(fixture, {**cfg, "view": "guests"}) if kind == "guest"]
assert shown == [100, 103, 101, 102], shown   # the template gets no page

# 9. Every page of every view renders, with gaps, odd guests and long names.
long_name = "a-guest-with-a-very-long-name-" * 3
odd = {**fixture, "resources": fixture["resources"] + [
    {"type": "qemu", "id": "qemu/104", "name": long_name, "status": "paused", "node": "pve1",
     "tags": ";".join(["tag"] * 60)},
    {"type": "lxc", "id": "lxc/105", "node": "pve1"}]}
many = {**fixture, "status": nodes(11)}
for sample in (fixture, odd, many, {**fixture, "zfs": {}, "net": {}, "temps": {}, "guest_rates": {}},
               {**fixture, "resources": None}, {**fixture, "status": None}, {}):
    for view in ("summary", "hosts", "guests"):
        trial = {**cfg, "view": view}
        for index in range(len(v.view_pages(sample, trial)) + 1):   # one extra: wraps round
            assert v.render(sample, trial, fixture["now"], index).size == (800, 480)
print("views OK")

# 10. A guest page must not hide a cluster problem, and stopped guests are not one.
def footer(data):
    d = mock.MagicMock()
    d.textlength.return_value = 0
    v.alert_footer(d, data, fixture["now"], cfg)
    return d.text.call_args.args[1]

assert footer(fixture) == "Cluster, replication and backups OK"
broken = {**fixture, "tasks": [{"type": "vzdump", "status": "ERROR", "endtime": 1, "node": "pve1"}],
          "status": [{"type": "cluster", "quorate": 0}, *fixture["status"][1:]]}
assert footer(broken).startswith("Cluster: NO QUORUM") and footer(broken).endswith("+1 more"), footer(broken)
print("footer OK")

# 11. Guest rates: bytes per second, and nothing for a guest whose totals restarted.
before = [{"id": "qemu/1", "type": "qemu", "netin": 100, "netout": 100, "diskread": 0, "diskwrite": 0},
          {"id": "qemu/2", "type": "qemu", "netin": 900, "netout": 0, "diskread": 0, "diskwrite": 0},
          {"id": "node/x", "type": "node"}]
after = [{"id": "qemu/1", "type": "qemu", "netin": 400, "netout": 100, "diskread": 30, "diskwrite": 0},
         {"id": "qemu/2", "type": "qemu", "netin": 5, "netout": 0, "diskread": 0, "diskwrite": 0},
         {"id": "node/x", "type": "node"}, {"id": "lxc/3", "type": "lxc"}]
assert v.guest_rates(before, after, 10) == {
    "qemu/1": {"netin": 30, "netout": 0, "diskread": 3, "diskwrite": 0}}
print("rates OK")

plain_api = lambda conf: mock.Mock(get=lambda path: {})

# 12. The loop turns pages faster than it fetches, and a second fetch gives rates.
clock = [1000.0]
sleeps, fetches, shown_pages = [], [], []
def fake_sleep(seconds):
    sleeps.append(seconds)
    clock[0] += seconds
    if len(sleeps) == 8:
        raise Stop
def fake_collect(api, conf):
    fetches.append(clock[0])
    return json.loads(json.dumps(fixture))
def fake_render(data, conf, now, index=0, paused=False):
    shown_pages.append((index, "guest_rates" in data))
    return real_render(data, conf, now, index)
real_render = v.render
paged = {**cfg, "view": "guests", "interval": 15, "page_seconds": 8}
screen = mock.MagicMock()
fixture_rates = fixture.pop("guest_rates")
with mock.patch.object(v, "find_screen", return_value=mock.MagicMock()), \
     mock.patch.object(v, "Api", plain_api), mock.patch.object(v, "collect", fake_collect), \
     mock.patch.object(v, "render", fake_render), \
     mock.patch.object(v.time, "time", lambda: clock[0]), \
     mock.patch.object(v.time, "sleep", fake_sleep):
    try:
        v.run(paged)
    except Stop:
        pass
fixture["guest_rates"] = fixture_rates
assert sleeps == [8] * 8, sleeps
assert fetches == [1000.0, 1016.0, 1032.0, 1048.0], fetches
assert [i for i, _ in shown_pages] == list(range(8)), shown_pages
assert shown_pages[0][1] is False and shown_pages[2][1] is True, shown_pages
print("paging OK")

# 13. One page: the loop keeps the old pace, so a two-node install does no extra work.
sleeps.clear()
with mock.patch.object(v, "find_screen", return_value=mock.MagicMock()), \
     mock.patch.object(v, "Api", plain_api), mock.patch.object(v, "collect", fake_collect), \
     mock.patch.object(v.time, "sleep", fake_sleep):
    try:
        v.run({**cfg, "interval": 0.001})
    except Stop:
        pass
assert set(sleeps) == {0.001}, sleeps
print("one page OK")

# 14. touch_test.py draws what to_frame would, and reads a simulated screen.
import builtins
import contextlib
import io
import os
import touch_test as t

for flip in (False, True):
    picture = Image.new("RGB", (800, 480))
    draw = ImageDraw.Draw(picture)
    boxes = [box for step in t.STEPS for box in step[2]]
    for left, top, right, bottom in boxes:
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(255, 255, 255))
    assert t.frame(boxes, flip) == v.to_frame(picture, flip), "touch targets are misplaced"

timeout = usb.core.USBTimeoutError("quiet")
def feed(*items):
    items = list(items)
    def read(*args):
        if not items:
            time.sleep(0.01)
            raise timeout
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return read

assert t.capture(feed(), 0.05) == []
assert t.capture(feed(b"\x01", timeout, b"\x02"), 5, quiet=0.02) == [b"\x01", b"\x02"]
assert t.changing_bytes([b"\x01\x10\x03", b"\x01\x20\x04"]) == [1, 2]

labels = [step[0] for step in t.STEPS]
full = {label: [b"\x01"] for label in labels} | {t.IDLE: []}
assert t.verdict(full)[0] is True
assert t.verdict({label: [] for label in labels})[1].startswith("NO REPORTS")
assert t.verdict({**full, "drag": []})[1] == "PARTIAL: touch responded, but not for: drag"
assert t.verdict({**full, t.IDLE: [b"\x00"]})[1].startswith("UNCLEAR")

def session(reads, argv=()):
    """main() from start to finish, against a fake screen in a temp folder."""
    device = mock.MagicMock()
    device.read.side_effect = reads
    endpoint = mock.MagicMock(wMaxPacketSize=64, bInterval=1)
    quick = tuple((label, task, boxes, 0.03) for label, task, boxes, _ in t.STEPS)
    out = io.StringIO()
    folder = tempfile.mkdtemp()
    with mock.patch.object(t, "STEPS", quick), mock.patch.object(t, "service_running", lambda: False), \
         mock.patch.object(t.usb.core, "find", return_value=device), \
         mock.patch.object(t.usb.util, "find_descriptor", return_value=endpoint), \
         mock.patch.object(t, "CAPTURE", folder + "/capture.txt"), \
         mock.patch.object(builtins, "input", lambda prompt: ""), \
         mock.patch.object(sys, "argv", ["touch_test.py", *argv]), \
         contextlib.redirect_stdout(out):
        try:
            t.main()
        except SystemExit as done:
            code = done.code
    return code, out.getvalue(), device, pathlib.Path(folder + "/capture.txt").read_text()

def silent(*args):
    time.sleep(0.005)
    raise timeout

code, text, device, log = session(silent)
assert code == 1 and "NO REPORTS" in text and log == "", text
assert device.write.call_count == len(t.STEPS) + 1      # a frame per step, then black
assert device.ctrl_transfer.call_args_list[0].args[4] == [0x00, 0x29], "panel not woken first"

count = [0]
def tapping(*args):
    count[0] += 1
    time.sleep(0.002)
    if count[0] < 3:                       # the idle step sees nothing
        raise timeout
    return bytes([count[0] % 256, 0x10, 0x00])
code, text, device, log = session(tapping)
assert code == 1 and "UNCLEAR" in text and "Bytes that changed: [0]" in text, text

with mock.patch.object(t, "service_running", lambda: True), mock.patch.object(sys, "argv", ["touch_test.py"]), \
     mock.patch.object(t.usb.core, "find", side_effect=AssertionError("opened a busy screen")):
    try:
        t.main()
    except SystemExit as done:
        assert "systemctl stop" in str(done.code)
print("touch test OK")

# 15. Live mode: the mpro.c layout, reports as they come, and no crash on a dead read.
#     xh has flag 2 and high nibble 1; yh has id 0 and high nibble 2.
report = bytes([0, 0, 1, 0x81, 0x23, 0x02, 0x45, 0, 0, 0, 0, 0, 0, 0])
assert t.decode(report) == "count 1 held point 0 x 291 y 581", t.decode(report)
# Real reports from a 480x800 panel: a held point, a release, the second point
# moved to the front, and the junk seen before the first good touch.
for raw, words in (("00000181b6026e0000ffffffffff", "count 1 held point 0 x 438 y 622"),
                   ("000000419002d10000ffffffffff", "count 0 up point 0 x 400 y 721"),
                   ("00000280cb10c6000080f3022300", "count 2 held point 1 x 203 y 198"),
                   ("d776ffde83efb791ed2ebebff453", "junk (count 255)")):
    assert t.decode(bytes.fromhex(raw)) == words, t.decode(bytes.fromhex(raw))
assert t.decode(b"\x01") == "too short to decode"
out = io.StringIO()
with contextlib.redirect_stdout(out):
    assert t.watch(feed(report, timeout, report), 0.03) == 2
assert out.getvalue().count("x 291 y 581") == 2, out.getvalue()
device = mock.MagicMock()
device.read.side_effect = usb.core.USBError("Overflow")
with mock.patch.object(t, "service_running", lambda: False), \
     mock.patch.object(t.usb.core, "find", return_value=device), \
     mock.patch.object(t.usb.util, "find_descriptor", return_value=mock.MagicMock()), \
     mock.patch.object(sys, "argv", ["touch_test.py", "--watch"]), \
     contextlib.redirect_stdout(io.StringIO()):
    try:
        t.main()
    except SystemExit as done:
        assert "failed" in str(done.code), done.code
assert device.read.call_args.args[:2] == (0x81, 14)
print("touch watch OK")

# 16. A tap ends the wait early; a burst or a held finger is one tap, not many.
def panel(*reads):
    device = mock.MagicMock()
    device.read.side_effect = feed(*reads)
    return v.Screen(device, 50), device

quiet, _ = panel()
start = time.monotonic()
assert quiet.wait(0.05) is None and time.monotonic() - start >= 0.05
burst, device = panel(*[report] * 5)
start = time.monotonic()
assert burst.wait(5) == "next" and time.monotonic() - start < 2
assert device.read.call_args.args[:2] == (0x81, 14)

def holding(*args):
    time.sleep(0.01)
    return report
held, device = panel()
device.read.side_effect = holding
start = time.monotonic()
assert held.wait(5) == "next" and 0.9 < time.monotonic() - start < 2

# 17. With touch on, the loop turns the page on a tap and survives an unplug there.
tapped = {**cfg, "touch": True, "view": "guests", "page_seconds": 30}
indexes = []
def note(data, conf, now, index=0, paused=False):
    indexes.append(index)
    return real_render(data, conf, now, index)
device = mock.MagicMock()
device.read.side_effect = feed(report, timeout, report, usb.core.USBError("gone"))
steps = [device, None]
quiet_log = io.StringIO()
with mock.patch.object(v, "find_screen", side_effect=lambda: steps.pop(0)), \
     mock.patch.object(v, "Api", plain_api), mock.patch.object(v, "collect", fake_collect), \
     mock.patch.object(v, "render", note), \
     mock.patch.object(v.time, "sleep", side_effect=Stop), \
     contextlib.redirect_stderr(quiet_log):
    try:
        v.run(tapped)
    except Stop:
        pass
assert indexes == [0, 1], indexes
assert "screen lost" in quiet_log.getvalue() and not steps
print("touch paging OK")

# 18. Pressure comes from the same rrddata rows; older Proxmox just has none.
rows = [{"pressurecpusome": 1, "pressurememorysome": 2, "pressureiosome": 3},
        {"pressurecpusome": 4.5, "pressurememorysome": 0, "pressureiosome": 12.5, "netin": 1, "netout": 2},
        {"pressurecpusome": None, "pressurememorysome": None, "pressureiosome": None}]
assert v.pressure(rows) == {"cpu": 4.5, "mem": 0, "io": 12.5}
assert v.pressure([{"netin": 1, "netout": 2}]) is None and off["pressure"]["pve1"] is None

class CountingApi(FakeApi):
    calls = 0
    def get(self, path):
        CountingApi.calls += "rrddata" in path
        return super().get(path)
v.collect(CountingApi(), cfg)
assert CountingApi.calls == 2, "one rrddata call per node, shared by rates and pressure"

class BrokenRrd(FakeApi):
    def get(self, path):
        if "rrddata" in path:
            raise OSError("no rrd")
        return super().get(path)
with contextlib.redirect_stderr(io.StringIO()):
    broken_rrd = v.collect(BrokenRrd(), cfg)
assert broken_rrd["net"]["pve1"] is None and broken_rrd["pressure"]["pve1"] is None

# 19. Storage: every type counts, the worst news wins, and old statuses still draw.
pool = [{"name": "tank", "health": "ONLINE", "size": 100, "alloc": 10}]
nfs = {"storage": "nas", "plugintype": "nfs", "status": "available", "disk": 80, "maxdisk": 100}
gone = {"storage": "pbs", "status": "unknown"}
empty = {"storage": "zero", "status": "available", "maxdisk": 0}
assert v.storage_state(pool, [nfs, empty]) == ("Storage OK", v.GOOD, 80)
assert v.storage_state(pool, [nfs, gone]) == ("DOWN: pbs", v.BAD, 80)
assert v.storage_state(None, [nfs])[:2] == ("ZFS ?", v.WARN)
assert v.storage_state([], []) == ("Storage OK", v.GOOD, None)
sick = [{"name": "tank", "health": "DEGRADED", "size": 100, "alloc": 10}]
assert v.storage_state(sick, [gone])[0] == "ZFS PROBLEM: tank"

many_stores = {**fixture, "resources": fixture["resources"] + [
    {"type": "storage", "node": "pve1", "storage": f"store-{i}" * 6, "plugintype": "cifs",
     "status": "available" if i % 2 else "unknown", "disk": 9e11, "maxdisk": 1e12} for i in range(19)]}
trial = {**cfg, "view": "guests"}
pages_found = [s for k, s in v.view_pages(many_stores, trial) if k == "storage" and s[0] == "pve1"]
assert [len(s[1]) for s in pages_found] == [8, 8, 6], pages_found
assert pages_found[0][1][0]["status"] == "unknown", "storage that is down goes first"
for view in ("summary", "hosts", "guests"):
    for index, _ in enumerate(v.view_pages(many_stores, {**cfg, "view": view})):
        v.render(many_stores, {**cfg, "view": view}, fixture["now"], index)
print("pressure and storage OK")

# 20. Tap zones, using real reports. Junk and repeats must not choose a direction.
left = bytes.fromhex("00000181000050000000ffffffff")     # along = 0x050 = 80
mid = bytes.fromhex("00000181000190000000ffffffff")      # along = 0x190 = 400
right = bytes.fromhex("000001810002d1000000ffffffff")    # along = 0x2d1 = 721
junk = bytes.fromhex("d776ffde83efb791ed2ebebff453")
assert [v.tap_action([r], None, False) for r in (left, mid, right)] == ["back", "pause", "next"]
assert [v.tap_action([r], None, True) for r in (left, mid, right)] == ["next", "pause", "back"]
assert v.tap_action([junk, junk], None, False) == "tap"
assert v.tap_action([right, right], right, False) == "tap", "a repeat of the last touch has no position"
assert v.tap_action([right, left], right, False) == "back", "the first fresh report decides"
# A quick tap, real reports from 20:32: only the release arrives, and it has a position.
assert v.tap_action([bytes.fromhex("000000408800750000ffffffffff")], None, False) == "back"
assert v.tap_action([bytes.fromhex("000000409d01b00000ffffffffff")], None, False) == "pause"
assert v.tap_action([bytes.fromhex("00000040e802ab0000ffffffffff")], None, False) == "next"
repeat = bytes.fromhex("00000040a400500000ffffffffff")
assert v.tap_action([repeat], repeat, False) == "tap", "a frozen release is still a repeat"
assert v.tap_action([bytes.fromhex("00000000a400500000ffffffffff")], None, False) == "tap", "count 0, not a release"

# 21. Navigation in the loop: back, pause holds the page, arrows still move it.
def drive(*actions, **extra):
    """Run the loop with these wait() answers and return (index, paused, pause) per draw."""
    answers = list(actions)
    drawn, pauses = [], []
    def wait(self, seconds, reverse=False):
        pauses.append((seconds, reverse))
        if not answers:
            raise Stop
        return answers.pop(0)
    def seen(data, conf, now, index=0, paused=False):
        drawn.append((index, paused))
        return real_render(data, conf, now, index, paused)
    conf = {**cfg, "touch": True, "view": "guests", "interval": 15, "page_seconds": 8, **extra}
    with mock.patch.object(v, "find_screen", return_value=mock.MagicMock()), \
         mock.patch.object(v, "Api", plain_api), mock.patch.object(v, "collect", fake_collect), \
         mock.patch.object(v, "render", seen), mock.patch.object(v.Screen, "wait", wait):
        try:
            v.run(conf)
        except Stop:
            pass
    return drawn, pauses

drawn, pauses = drive("next", "back", "back", "pause", None, "next", "tap", "pause", None)
assert drawn == [(0, False), (1, False), (0, False), (-1, False), (-1, True), (-1, True),
                 (0, True), (0, False), (0, True), (0, True)], drawn   # a "tap" resumes
assert [p[0] for p in pauses[:6]] == [8, 8, 8, 8, 15, 15], pauses   # paused: refresh pace
image = v.render(fixture, {**cfg, "view": "guests"}, fixture["now"], -1, True)
assert image.size == (800, 480)      # -1 wraps to the last page
assert drive(None)[1][0][1] is False
assert drive(None, flip=True)[1][0][1] is True
assert drive(None, flip=True, touch_invert=True)[1][0][1] is False
print("navigation OK")

# 22. The journal says whether touch is on and what each tap did.
log = io.StringIO()
with contextlib.redirect_stderr(log):
    logged, _ = panel(left, timeout)
    assert logged.wait(1) == "back"
    drive(None)
assert "tap: back, 1 reports, first 0000018100" in log.getvalue(), log.getvalue()
assert "starting: view guests, touch on" in log.getvalue(), log.getvalue()
print("touch logging OK")

# 23. Guest pressure comes from that guest's status, and only if all of it is there.
vm = {"id": "qemu/100", "vmid": 100, "type": "qemu", "node": "pve1"}
asked = []
def status_api(reply):
    return mock.Mock(get=lambda path: asked.append(path) or reply)
full_state = {"pressurecpusome": "2", "pressurememorysome": "0.00", "pressureiosome": "14"}
assert v.guest_pressure(status_api(full_state), vm) == {"cpu": 2, "mem": 0, "io": 14}
assert asked == ["/nodes/pve1/qemu/100/status/current"]
assert v.guest_pressure(status_api({"status": "running"}), vm) is None
# status/current really does send text: this crashed the service on a live VM.
live = v.guest_pressure(status_api(full_state), vm)
assert v.pressure_row(live, cfg) == ("CPU 2   Mem 0   IO 14", v.WARN)
guest_page = {**fixture, "guest_pressure": {"qemu/100": live}}
v.render(guest_page, {**cfg, "view": "guests"}, fixture["now"], 2)
with contextlib.redirect_stderr(io.StringIO()):
    assert v.attempt(v.guest_pressure, status_api({"pressurecpusome": "n/a",
                     "pressurememorysome": "0", "pressureiosome": "0"}), vm) is None
assert v.pressure_row(None, cfg) == ("—", v.DIM)
assert v.pressure_row({"cpu": 1, "mem": 0, "io": 3}, cfg) == ("CPU 1   Mem 0   IO 3", v.TEXT)
assert v.pressure_row({"cpu": 1, "mem": 0, "io": 14}, cfg)[1] == v.WARN
assert v.pressure_row({"cpu": 45, "mem": 0, "io": 3}, cfg)[1] == v.BAD

# 24. hide: rows and pages go, drawing survives everything hidden, and nothing leaks.
def words(page, conf):
    d = mock.MagicMock()
    d.textlength.return_value = 0
    v.guest_detail(d, page, fixture["guest_rates"].get(page["id"]),
                   fixture["guest_pressure"].get(page["id"]), conf)
    return [call.args[1] for call in d.text.call_args_list]

running_guest = next(r for r in fixture["resources"] if r.get("vmid") == 100)
assert {"Net", "Disk I/O", "Pressure", "Tags"} <= set(words(running_guest, cfg))
quiet_page = words(running_guest, {**cfg, "hide": ["net", "disk_io", "pressure", "tags"]})
assert not {"Net", "Disk I/O", "Pressure", "Tags"} & set(quiet_page), quiet_page
assert "Pressure" not in words(next(r for r in fixture["resources"] if r.get("vmid") == 101), cfg)

def card(conf):
    d = mock.MagicMock()
    d.textlength.return_value = 0
    v.draw_node(d, 0, "pve1", fixture, {"pve1": True},
                {r.get("node"): r for r in fixture["resources"] if r.get("type") == "node"}, conf)
    return [call.args[1] for call in d.text.call_args_list]

assert {"Net", "Temp", "Pressure"} <= set(card(cfg))
assert not {"Net", "Temp", "Pressure"} & set(card({**cfg, "hide": ["net", "temp", "pressure"]}))
hidden = {**cfg, "view": "guests", "hide": list(v.HIDEABLE)}
assert [k for k, _ in v.view_pages(fixture, hidden)] == ["host", "guest", "guest"] * 2
for view in ("summary", "hosts", "guests"):
    conf = {**cfg, "view": view, "hide": list(v.HIDEABLE)}
    for index, _ in enumerate(v.view_pages(fixture, conf)):
        v.render(fixture, conf, fixture["now"], index)
print("hide OK")

# 25. Load and swap: from the rows, drawn on existing lines, and amber when swap is high.
assert v.load_and_swap([{"loadavg": 9, "swapused": 1, "swaptotal": 2},
                        {"loadavg": 0.5, "swapused": 3, "swaptotal": 4}, {"netin": 1}]) == \
    {"loadavg": 0.5, "swapused": 3, "swaptotal": 4}
assert v.load_and_swap([{"netin": 1}]) is None
assert "load" in off and off["load"]["pve1"] is None

def card_calls(conf, data=fixture):
    d = mock.MagicMock()
    d.textlength.return_value = 0
    v.draw_node(d, 0, "pve1", data, {"pve1": True},
                {r.get("node"): r for r in fixture["resources"] if r.get("type") == "node"}, conf)
    return {call.args[1]: call.kwargs.get("fill") for call in d.text.call_args_list}

shown_text = card_calls(cfg)
assert "load 0.42" in shown_text and shown_text["swap 0.1/8.0 GB"] == v.DIM, shown_text
assert not any(str(t).startswith(("load", "swap")) for t in card_calls({**cfg, "hide": ["load", "swap"]}))
swapping = {**fixture, "load": {"pve1": {"loadavg": 1, "swapused": 6e9, "swaptotal": 8e9}}}
assert card_calls(cfg, swapping)["swap 5.6/7.5 GB"] == v.WARN
no_swap = {**fixture, "load": {"pve1": {"loadavg": 1, "swapused": 0, "swaptotal": 0}}}
assert not any(str(t).startswith("swap") for t in card_calls(cfg, no_swap))
assert not any(str(t).startswith(("load", "swap")) for t in card_calls(cfg, {**fixture, "load": {}}))

# 26. One status call per page, only for a running guest, and never when hidden.
def status_calls(pages_shown, **extra):
    calls = []
    def fake_get(path):
        calls.append(path)
        return dict(full_state)
    def wait(self, seconds, reverse=False):
        if len(waits) >= pages_shown:
            raise Stop
        waits.append(1)
        return "next"
    waits = []
    conf = {**cfg, "touch": True, "view": "guests", **extra}
    with mock.patch.object(v, "find_screen", return_value=mock.MagicMock()), \
         mock.patch.object(v, "Api", lambda c: mock.Mock(get=fake_get)), \
         mock.patch.object(v, "collect", fake_collect), mock.patch.object(v.Screen, "wait", wait):
        try:
            v.run(conf)
        except Stop:
            pass
    return calls

# pages: pve1, its storage, vm 100, stopped ct 103, pve2, its storage, ct 101, vm 102
assert status_calls(3) == ["/nodes/pve1/qemu/100/status/current"]
assert status_calls(4) == ["/nodes/pve1/qemu/100/status/current"], "stopped guest asked"
assert status_calls(7) == ["/nodes/pve1/qemu/100/status/current",
                           "/nodes/pve2/lxc/101/status/current",
                           "/nodes/pve2/qemu/102/status/current"]
assert status_calls(7, hide=["pressure"]) == []
print("load, swap and guest pressure OK")

# 27. The frames test: bins by time, frames sent on schedule, in both read styles.
def spaced(*args):
    time.sleep(0.01)
    return b"x"

sent = []
bins = t.run_phase(spaced, lambda: sent.append(1), True, False, seconds=1, every=1)
assert sent == [1] and len(bins) == 1 and bins[0] > 0, (sent, bins)
sent.clear()
bins = t.run_phase(spaced, lambda: sent.append(1), True, True, seconds=1, every=1)
assert sent == [1] and bins[0] > 0, (sent, bins)
sent.clear()
assert t.run_phase(spaced, lambda: sent.append(1), False, False, seconds=1, every=1)[0] > 0
assert not sent
def dead(*args):
    raise usb.core.USBError("gone")
for background in (False, True):
    try:
        t.run_phase(dead, lambda: None, False, background, seconds=1, every=1)
    except usb.core.USBError:
        pass
    else:
        raise AssertionError("a dead endpoint went unnoticed")
print("frames test OK")

# 28. --frames runs start to finish with nobody at the keyboard, and saves its results.
device = mock.MagicMock()
device.read.side_effect = spaced
folder = tempfile.mkdtemp()
with mock.patch.object(t, "service_running", lambda: False), \
     mock.patch.object(t.usb.core, "find", return_value=device), \
     mock.patch.object(t.usb.util, "find_descriptor", return_value=mock.MagicMock(wMaxPacketSize=64, bInterval=0)), \
     mock.patch.object(t, "LEAD_SECONDS", 0), mock.patch.object(t, "GAP_SECONDS", 0), \
     mock.patch.object(t, "PHASE_SECONDS", 1), mock.patch.object(t, "FRAMES_LOG", folder + "/f.txt"), \
     mock.patch.object(t.run_phase, "__defaults__", (1, 1)), \
     mock.patch.object(builtins, "input", side_effect=AssertionError("waited for Enter")), \
     mock.patch.object(sys, "argv", ["touch_test.py", "--frames"]), \
     contextlib.redirect_stdout(io.StringIO()):
    try:
        t.main()
    except SystemExit as done:
        assert done.code == 0, done.code
saved = pathlib.Path(folder + "/f.txt").read_text().splitlines()
assert len(saved) == 3 and saved[1].startswith("2/3 frames, reader always waiting: ["), saved
print("hands-free frames test OK")

# 29. --revive: hands-free, stops at the first thing that works, and survives a failed reset.
def revive(read_effect, reset_effect=None):
    device = mock.MagicMock()
    device.read.side_effect = read_effect
    device.reset.side_effect = reset_effect
    log = tempfile.mkdtemp() + "/r.txt"
    with mock.patch.object(t, "service_running", lambda: False), \
         mock.patch.object(t.usb.core, "find", return_value=device), \
         mock.patch.object(t.usb.util, "find_descriptor", return_value=mock.MagicMock(wMaxPacketSize=64, bInterval=0)), \
         mock.patch.object(t, "REVIVE_LEAD", 0), mock.patch.object(t, "REVIVE_WATCH", 1), \
         mock.patch.object(t, "REVIVE_TRY", 1), mock.patch.object(t, "REVIVE_REST", 0), \
         mock.patch.object(t, "GAP_SECONDS", 0), mock.patch.object(t, "REVIVE_LOG", log), \
         mock.patch.object(t.run_phase, "__defaults__", (1, 1)), \
         mock.patch.object(t.time, "sleep", lambda s: None), \
         mock.patch.object(builtins, "input", side_effect=AssertionError("waited for Enter")), \
         mock.patch.object(sys, "argv", ["touch_test.py", "--revive"]), \
         contextlib.redirect_stdout(io.StringIO()):
        try:
            t.main()
        except SystemExit as done:
            assert done.code == 0, done.code
    return pathlib.Path(log).read_text().splitlines(), device

def pacing_nothing(*args):
    import time as clock
    start = clock.monotonic()
    while clock.monotonic() - start < 0.005:
        pass
    raise timeout
lines, device = revive(pacing_nothing)
assert [l.split(":")[0] for l in lines[1:]] == ["wake command", "set configuration", "usb reset"], lines
assert device.reset.called
lines, device = revive(pacing_nothing, usb.core.USBError("no device"))
assert lines[-1].startswith("usb reset: failed"), lines

calls = [0]
def back_after_wake(*args):
    calls[0] += 1
    if calls[0] < 40:
        pacing_nothing()
    return b"x"
lines, device = revive(back_after_wake)
assert any(l.startswith("wake command, second time") for l in lines) or not device.reset.called, lines
print("revive test OK")

# 30. Waiting for taps keeps touch awake, at the panel's brightness, and only then.
wakes = lambda device: [c.args[4] for c in device.ctrl_transfer.call_args_list if c.args[4] == [0x00, 0x29]]
screen_now, device = panel()
device.ctrl_transfer.reset_mock()
with mock.patch.object(v, "TOUCH_WAKE", 0.03):
    screen_now.wait(0.1)
assert 2 <= len(wakes(device)) <= 5, wakes(device)
assert [0x00, 0x51, 0x02, 0, 0, 0, 50, 0] in [c.args[4] for c in device.ctrl_transfer.call_args_list]
device.ctrl_transfer.reset_mock()
screen_now.show(b"x")
assert not wakes(device), "a frame must not wake by itself"
lost = mock.MagicMock()
gone_screen = v.Screen(lost, 50)
lost.ctrl_transfer.side_effect = usb.core.USBError("gone")
try:
    gone_screen.wait(1)
except usb.core.USBError:
    pass
else:
    raise AssertionError("an unplug during wake must reach run()")
print("touch keep-awake OK")

# 32. Frozen touch: real reports from 20:13. A USB reset did not help on the real panel,
#     so never try one, and a tap with no position must still get out of a pause.
frozen = bytes.fromhex("00000040a400500000ffffffffff")
stuck, device = panel(frozen, timeout, frozen, timeout, frozen, timeout)
with contextlib.redirect_stderr(io.StringIO()):
    assert [stuck.wait(1) for _ in range(3)] == ["back", "tap", "tap"]   # the first can't be told apart
assert not device.reset.called, "a USB reset blanks the screen and does not help"
drawn, _ = drive("pause", "tap", "tap")
assert drawn == [(0, False), (0, True), (0, False), (1, False)], drawn
print("frozen touch OK")

# 33. --thaw: hands-free, frozen baseline first, stops at the first cure, survives a failure.
def thaw(read_effect, disconnect_fails=False):
    device = mock.MagicMock()
    device.read.side_effect = read_effect
    folder = pathlib.Path(tempfile.mkdtemp())
    switch = folder / "authorized"
    switch.write_text("1")
    finder = mock.Mock(side_effect=OSError("no sysfs")) if disconnect_fails else mock.Mock(return_value=switch)
    with mock.patch.object(t, "service_running", lambda: False), \
         mock.patch.object(t.usb.core, "find", return_value=device), \
         mock.patch.object(t.usb.util, "find_descriptor", return_value=mock.MagicMock(wMaxPacketSize=64, bInterval=0)), \
         mock.patch.object(t, "THAW_LEAD", 0), mock.patch.object(t, "THAW_TRY", 0.05), \
         mock.patch.object(t, "THAW_LOG", str(folder / "t.txt")), \
         mock.patch.object(t, "authorized_file", finder), \
         mock.patch.object(t.time, "sleep", lambda s: None), \
         mock.patch.object(builtins, "input", side_effect=AssertionError("waited for Enter")), \
         mock.patch.object(sys, "argv", ["touch_test.py", "--thaw"]), \
         contextlib.redirect_stdout(io.StringIO()):
        try:
            t.main()
        except SystemExit as done:
            assert done.code == 0, done.code
    return (folder / "t.txt").read_text().splitlines(), device

def repeat_frozen(*args):
    import time as clock
    start = clock.monotonic()
    while clock.monotonic() - start < 0.004:
        pass
    return frozen

lines, device = thaw(repeat_frozen)
assert [l.split(":")[0] for l in lines] == ["nothing, to see the frozen state", "display off and on",
                                            "USB disconnect"], lines
assert " 1 different" in lines[0] and [0x00, 0x28] in [c.args[4] for c in device.ctrl_transfer.call_args_list]
lines, _ = thaw(repeat_frozen, disconnect_fails=True)
assert lines[-1] == "USB disconnect: failed: no sysfs", lines

fresh_after = [0]
def cured_by_display(*args):
    fresh_after[0] += 1
    report_now = repeat_frozen()
    return report_now if fresh_after[0] < 15 else bytes([fresh_after[0] % 256]) * 14
lines, _ = thaw(cured_by_display)
assert len(lines) == 2 and lines[1].startswith("display off and on"), lines

here = pathlib.Path(tempfile.mkdtemp()) / "1-2"
here.mkdir()
(here / "busnum").write_text("1\n")
(here / "devnum").write_text("7\n")
with mock.patch.object(t.pathlib, "Path", lambda value: here.parent):
    assert t.authorized_file(mock.Mock(bus=1, address=7)) == here / "authorized"
    try:
        t.authorized_file(mock.Mock(bus=1, address=8))
    except OSError:
        pass
    else:
        raise AssertionError("a missing device went unnoticed")
print("thaw test OK")
