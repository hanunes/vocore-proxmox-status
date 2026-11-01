# Agent notes

Start with `README.md` for setup, settings and the screen protocol. This file covers what the code alone won't tell you.

## Commands

- `python3 test_status.py` runs all the tests. It needs no screen or Proxmox.
- `python3 docs/make_screens.py` regenerates the README screenshots. Run it after any layout change.

Dependencies are `python3-pil`, `python3-usb` and `fonts-dejavu-core`. Code must run on Python 3.11, the version in PVE 8.

## Rules

- Keep one loop and no threads. A hung fetch must stop the clock, because the clock is the freeze detector.
- The service only reads from Proxmox. Never add anything that writes to it.
- A section that fails shows `?` or `—`. It must never stop the display.
- With no screen attached, the service must not touch the API or the token.
- Use only the standard library, Pillow and pyusb.
- Defaults must keep today's behaviour, because `install.sh` never overwrites `config.toml`.
- Passing tests don't prove hardware behaviour. The touch faults only appeared on a real screen, so say when something
  hasn't been tested on one.

## Proxmox API

- `status/current` returns pressure as text, while `rrddata` returns numbers.
- `/cluster/resources` has no guest pressure, so fetch it only for the guest on screen. Don't add a call per guest per
  refresh.

## Touch

- Read 14 bytes from endpoint `0x81`, and resend the wake command every 3 seconds.
- A quick tap can arrive as a lone release report (count 0, state 1) that still has its position.
- Touch can freeze so every report repeats an old one. A USB reset, a software USB disconnect and turning the display off
  and on don't clear it, and only a replug has. Don't add those back as fixes.

## Publishing

- `TODO.md` is local only.
- Never commit tokens, `--dump` output or real node names. `fixture.json` must stay made-up data.
