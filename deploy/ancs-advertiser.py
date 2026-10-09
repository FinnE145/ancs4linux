"""Keep this machine reachable for the bonded iPhone over LE, and keep pairing closed.

Run by ancs4linux-advertise.service (as root) with the ancs4linux venv's Python.

- Registers an LE advertisement with bluetoothd that solicits ANCS: seeing it, the bonded
  iPhone connects on its own. Registered again whenever bluetoothd restarts or releases it.
  (Needs BlueZ >= 5.87 on kernel 6.8.0-142: older bluetoothd sent a padded Add Extended
  Advertising Data command that the kernel rejects.)
- Outside an ancs-pair window, keeps the adapter non-pairable, non-discoverable and closed to
  classic (BR/EDR) connections, correcting any change as soon as bluetoothd reports it, plus
  a periodic recheck. ancs-pair marks its window in PAIRING_WINDOW (the window's end time),
  so a killed ancs-pair can't leave it open. While the adapter is pairable, ancs4linux's
  agent accepts any pairing request.
- Repairs stale links: when the kernel holds an LE link to a paired phone that bluetoothd
  says isn't connected (a bluetoothd 5.87 race when the phone reconnects within a fraction
  of a second of a disconnect: it ignores the new connection, then clears the old one), no
  notifications can flow on it and nothing else ever notices. Seen on two consecutive
  checks, the link is closed so the phone reconnects cleanly.
- Pings systemd's watchdog from the main loop.

Logs only registrations and corrections.
"""
import logging
import os
import re
import socket
import subprocess
import time
from typing import Any, Dict, List, Tuple

from dasbus.connection import SystemMessageBus
from dasbus.loop import EventLoop
from dasbus.server.interface import dbus_interface
from dasbus.typing import Bool, Str, Variant, get_variant
from gi.repository import GLib

log = logging.getLogger("ancs-advertiser")

ADAPTER = os.environ.get("ADAPTER_PATH", "/org/bluez/hci0")
ADVERT_PATH = "/fe_pro/ancs_advertisement"
ANCS_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
PAIRING_WINDOW = "/run/ancs4linux-pairing"
CALL_TIMEOUT_MS = 10_000  # dasbus's default is to wait forever
RETRY_SECONDS = 5
RECHECK_SECONDS = 30
STALE_CHECK_SECONDS = 5  # a real connection reaches bluetoothd in well under 1 s
# Adapter properties that must stay off outside a pairing window.
CLOSED = ("Pairable", "Discoverable", "Connectable")


@dbus_interface("org.bluez.LEAdvertisement1")
class Advertisement:
    def __init__(self, on_release):
        self.on_release = on_release

    @property
    def Type(self) -> Str:
        return "peripheral"

    @property
    def SolicitUUIDs(self) -> List[Str]:
        return [ANCS_UUID]

    @property
    def Discoverable(self) -> Bool:
        # LE General Discoverable flag in the advertisement only; the adapter itself
        # (classic inquiry, iOS Settings listing) stays non-discoverable.
        return True

    @property
    def Includes(self) -> List[Str]:
        return ["local-name"]

    def Release(self) -> None:
        log.warning("bluetoothd released the advertisement; registering again")
        self.on_release()


class Advertiser:
    def __init__(self, bus: SystemMessageBus):
        self.bus = bus
        self.registered = False
        self.retry_pending = False
        # LE handle -> (check round, time.monotonic()) of its first stale sighting.
        self.stale_suspects: Dict[str, Tuple[int, float]] = {}
        self.stale_round = 0
        self.adapter = bus.get_proxy("org.bluez", ADAPTER)
        self.properties = bus.get_proxy(
            "org.bluez", ADAPTER, interface_name="org.freedesktop.DBus.Properties"
        )
        bus.publish_object(ADVERT_PATH, Advertisement(self.released))
        dbus = bus.get_proxy("org.freedesktop.DBus", "/org/freedesktop/DBus")
        dbus.NameOwnerChanged.connect(self.name_owner_changed)
        self.properties.PropertiesChanged.connect(self.adapter_changed)

    # --- advertisement -------------------------------------------------------------
    def register(self) -> bool:
        self.retry_pending = False
        if self.registered:
            return False

        def done(call: Any) -> None:
            try:
                call()
            except Exception as e:
                log.warning(f"RegisterAdvertisement failed: {e}; retrying in {RETRY_SECONDS} s")
                self.schedule_register()
                return
            self.registered = True
            log.info("advertisement registered")

        try:
            self.adapter.RegisterAdvertisement(
                ADVERT_PATH, {}, callback=done, timeout=CALL_TIMEOUT_MS
            )
        except Exception as e:
            log.warning(f"RegisterAdvertisement failed: {e}; retrying in {RETRY_SECONDS} s")
            self.schedule_register()
        return False

    def schedule_register(self) -> None:
        if not self.retry_pending:
            self.retry_pending = True
            GLib.timeout_add_seconds(RETRY_SECONDS, self.register)

    def released(self) -> None:
        self.registered = False
        self.schedule_register()

    def name_owner_changed(self, name: str, old: str, new: str) -> None:
        if name != "org.bluez":
            return
        # bluetoothd went away or came back: its registrations are gone either way.
        self.registered = False
        if new:
            log.info("bluetoothd (re)started; registering again")
            self.schedule_register()
            # One-shot (enforce_closed returns True for the periodic recheck).
            GLib.timeout_add_seconds(RETRY_SECONDS, lambda: self.enforce_closed() and False)

    # --- adapter state -------------------------------------------------------------
    @staticmethod
    def pairing_window_open() -> bool:
        try:
            with open(PAIRING_WINDOW) as f:
                return time.time() < float(f.read().strip() or 0)
        except (OSError, ValueError):
            return False

    def close(self, prop: str) -> None:
        def done(call: Any) -> None:
            try:
                call()
            except Exception as e:
                log.warning(f"couldn't turn {prop} off: {e}")

        self.properties.Set(
            "org.bluez.Adapter1", prop, get_variant(Bool, False),
            callback=done, timeout=CALL_TIMEOUT_MS,
        )

    def adapter_changed(self, interface: str, changes: Dict[str, Variant], _: Any) -> None:
        if interface != "org.bluez.Adapter1" or self.pairing_window_open():
            return
        for prop in CLOSED:
            if prop in changes and changes[prop].unpack():
                log.warning(f"{prop} turned on outside a pairing window; turning it off")
                self.close(prop)
        if changes.get("Powered") is not None and changes["Powered"].unpack():
            self.enforce_closed()

    def enforce_closed(self) -> bool:
        if self.pairing_window_open():
            return True

        def done(call: Any) -> None:
            try:
                state = call()
            except Exception as e:
                log.warning(f"couldn't read adapter state: {e}")
                return
            for prop in CLOSED:
                if prop in state and state[prop].unpack():
                    log.warning(f"{prop} was on outside a pairing window; turning it off")
                    self.close(prop)

        try:
            self.properties.GetAll("org.bluez.Adapter1", callback=done, timeout=CALL_TIMEOUT_MS)
            # Also catches the advertisement having been dropped without a Release.
            self.properties.Get(
                "org.bluez.LEAdvertisingManager1", "ActiveInstances",
                callback=self.check_instances, timeout=CALL_TIMEOUT_MS,
            )
        except Exception as e:
            log.warning(f"couldn't read adapter state: {e}")
        return True  # keep the periodic recheck running

    def check_instances(self, call: Any) -> None:
        try:
            active = call()
            active = active.unpack() if hasattr(active, "unpack") else active
        except Exception:
            return
        if self.registered and active == 0:
            log.warning("no active advertisement although registered; registering again")
            self.registered = False
            self.schedule_register()


    # --- stale links ----------------------------------------------------------------
    def check_stale_links(self) -> bool:
        self.stale_round += 1
        this_round = self.stale_round
        try:
            out = subprocess.run(
                ["hcitool", "con"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception as e:
            log.warning(f"couldn't list connections: {e}")
            return True
        links = re.findall(r"> LE ([0-9A-F:]{17}) handle (\d+)", out)
        current = {handle for _, handle in links}
        self.stale_suspects = {h: n for h, n in self.stale_suspects.items() if h in current}
        for mac, handle in links:
            device = f"{ADAPTER}/dev_{mac.replace(':', '_')}"
            props = self.bus.get_proxy(
                "org.bluez", device, interface_name="org.freedesktop.DBus.Properties"
            )

            def done(call: Any, mac: str = mac, handle: str = handle, rnd: int = this_round) -> None:
                try:
                    state = call()
                except Exception:
                    return  # not a device bluetoothd knows; not ours to judge
                paired = state.get("Paired")
                connected = state.get("Connected")
                if not (paired and paired.unpack()) or connected is None or connected.unpack():
                    self.stale_suspects.pop(handle, None)
                    return
                # Act only on sightings from two different check rounds at least
                # STALE_CHECK_SECONDS apart: late replies to two checks can arrive together.
                first = self.stale_suspects.get(handle)
                if first is None:
                    self.stale_suspects[handle] = (rnd, time.monotonic())
                    return
                if rnd != first[0] and time.monotonic() - first[1] >= STALE_CHECK_SECONDS - 1:
                    log.warning(
                        f"stale LE link to {mac} (handle {handle}): kernel connected, "
                        "bluetoothd not; closing it so the phone reconnects cleanly"
                    )
                    self.stale_suspects.pop(handle, None)
                    subprocess.run(["hcitool", "ledc", handle], timeout=5)

            props.GetAll("org.bluez.Device1", callback=done, timeout=CALL_TIMEOUT_MS)
        return True


def start_watchdog() -> None:
    usec = os.environ.get("WATCHDOG_USEC")
    path = os.environ.get("NOTIFY_SOCKET")
    if not usec or not path:
        return
    address = "\0" + path[1:] if path.startswith("@") else path
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def ping() -> bool:
        sock.sendto(b"WATCHDOG=1", address)
        return True

    ping()
    GLib.timeout_add(max(1000, int(usec) // 3000), ping)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    bus = SystemMessageBus()
    advertiser = Advertiser(bus)
    advertiser.register()
    advertiser.enforce_closed()
    GLib.timeout_add_seconds(RECHECK_SECONDS, advertiser.enforce_closed)
    GLib.timeout_add_seconds(STALE_CHECK_SECONDS, advertiser.check_stale_links)
    start_watchdog()
    EventLoop().run()


if __name__ == "__main__":
    main()
