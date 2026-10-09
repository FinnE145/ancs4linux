# deploy/: running this fork as a system service

This is how this fork runs on a home server (Ubuntu 24.04) that reads one iPhone's notifications
and hands them to other programs over the system D-Bus. It isn't a desktop integration: nothing
here shows notifications itself.

## What's here

| file | what |
|---|---|
| `install.sh` | installs everything (run with `sudo`; safe to re-run): upstream at a pinned commit plus `patches/*.patch` in `/opt/ancs4linux/src`, a venv in `/opt/ancs4linux/venv`, the units, D-Bus policy and `/etc/ancs4linux.env` |
| `patches/` | the observer changes on this branch, exported with `git format-patch` (see the commit messages for the reasons) |
| `ancs4linux-observer.service` | the observer (upstream daemon + patches), with a systemd watchdog |
| `ancs4linux-advertising.service` | upstream's advertising daemon, used here only as the pairing agent |
| `ancs4linux-advertise.service`, `ancs-advertiser.py` | keeps an ANCS-soliciting LE advertisement registered so the bonded phone reconnects on its own, keeps pairing closed outside `ancs-pair`, and repairs stale LE links (see the docstring) |
| `ancs-pair` | opens a pairing window (`sudo ancs-pair [seconds]`, default 180) and prints the pairing code |
| `ancs4linux-logger.service`, `ancs-logger.py` | debug consumer: writes the observer's signals as JSON lines to `/var/log/ancs4linux/` (bodies only with `LOG_BODIES=1`) |
| `ancs4linux.env` | settings, installed to `/etc/ancs4linux.env` on first install |

## Requirements

- **BlueZ ≥ 5.87.** `install.sh` refuses older versions: on kernel 6.8.0-142, older bluetoothd
  can't register LE advertisements. Patch `0009` also works around a BlueZ gatt-client
  use-after-free that is fixed upstream after 5.87 ("shared/gatt-client: Fix calling destroy
  after unregistering notify"). A BlueZ build with that fix is recommended.
- A Bluetooth LE adapter, `python3-gi` from apt.

## Settings (`/etc/ancs4linux.env`)

- `LOG_BODIES` — `1` logs full message bodies in the debug log. Debugging only.
- `ADAPTER_PATH` — adapter to use, default `/org/bluez/hci0`.
- `ANCS_USER` — who runs the debug logger and joins group `ancs4linux` (which may call the
  observer's methods). Default: the user who ran `sudo install.sh`.

## Pairing

`sudo ancs-pair`, then on the iPhone: Settings → Bluetooth → this machine. Check the code, tap
Pair, allow notifications. iOS keeps the link over LE afterwards (classic Bluetooth is closed
outside pairing windows, because over classic the link drops every ~30 s). Don't tap the device
in iOS Settings to "connect": that tries classic.

## The observer's interface

On the system bus as `ancs4linux.Observer`, object `/`. Changed from upstream: notifications carry
everything ANCS reports (event, flags, category, subtitle, date, message size), ids are stable
across reconnects, `Subscribed` marks the start of a complete list after every (re)connection, and
there are `Connected` / `Disconnected` / `SubscribeFailed` signals and `GetStatus()` /
`GetNotifications()` methods. The observer only reports facts and never filters or changes
notification data; deciding what to show is up to its consumers. Details are in the commit
messages and the comments in `ancs4linux/observer/server.py`.
