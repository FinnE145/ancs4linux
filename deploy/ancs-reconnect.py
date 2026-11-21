"""Bring the bonded iPhone back when it has stopped reconnecting, and find out why it stopped.

Run by ancs4linux-reconnect.timer every 10 minutes (as root: it stops and restarts units),
on the system python3 with apt's python3-gi.

iOS sometimes stops reconnecting for days although fe-pro still holds the bond and still
advertises. Every time before 2026-11-21 that was fixed by re-pairing. On 2026-11-21 one restart
of bluetooth.service brought the phone back in 3 s, after three days without a connection
attempt. bluetoothd had been discovering the whole time (the observer scans while the phone is
away) without one sighting. So the long-running scan is a suspect, but not proven.

While no paired phone is connected and subscribed, this counts from when it first saw that.
After DOWN_HOURS, and DOWN_HOURS after each attempt, it makes an attempt, MAX_ATTEMPTS at
most (Finn, 2026-11-21: the phone is likely to be home for one 6 hour stretch in 36 hours),
then waits for the phone to come back on its own. A phone that is simply away loses nothing:
it isn't connected to anything here.

An attempt has two stages, so each one also answers the scan question:
1. Stop the observer for up to WAIT_SECONDS. Its discovery session ends with it, so
   bluetoothd stops scanning, while bluetoothd and the advertisement stay as they were. If
   the phone connects now, the scan was what kept it out.
2. Otherwise restart bluetooth.service (it takes the observer and both advertising units with
   it) and wait again.
Each attempt logs one `RESULT` line (`journalctl -u ancs4linux-reconnect | grep RESULT`) and,
before it, the state that explains it: Discovering, the advertising instances, and how many
HCI events the controller sent in 10 s (an LE scan in a house hears dozens of devices).

State (when the phone went, the attempts) is in /run/ancs4linux-reconnect, so a reboot starts
the count again. Logs no notification content: it reads only the status's connection fields.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

DOWN_HOURS = 6
MAX_ATTEMPTS = 6
WAIT_SECONDS = 120
POLL_SECONDS = 2
STATE_FILE = "/run/ancs4linux-reconnect/state.json"
ADAPTER = os.environ.get("ADAPTER_PATH", "/org/bluez/hci0")
HCI = ADAPTER.rsplit("/", 1)[-1]
CALL_TIMEOUT_MS = 10_000
# ActiveInstances is on the advertising manager, not Adapter1 (asking Adapter1 gave InvalidArgs
# at the first install, 2026-11-21).
ADVERTISING = "org.bluez.LEAdvertisingManager1"


# --- the decision (pure) ------------------------------------------------------------------------
def decide(state: Dict[str, Any], live: bool, now: float) -> Tuple[Dict[str, Any], str]:
    """The next state and what to do: "none", "recovered", "attempt" or "gave_up".

    state is {} while the phone is live, else {"down_since", "attempts", "last_attempt"}.
    """
    if live:
        return {}, ("recovered" if state else "none")
    if not state:
        return {"down_since": now, "attempts": 0, "last_attempt": None}, "none"
    attempts = state["attempts"]
    if attempts >= MAX_ATTEMPTS:
        return state, "none"
    since = state["last_attempt"] if state["last_attempt"] is not None else state["down_since"]
    if now - since < DOWN_HOURS * 3600:
        return state, "none"
    new = dict(state, attempts=attempts + 1, last_attempt=now)
    return new, ("gave_up" if attempts + 1 == MAX_ATTEMPTS else "attempt")


def live_from_status(status: Optional[Dict[str, Any]]) -> bool:
    """A paired phone connected and subscribed, by the observer's GetStatus (None: no answer)."""
    if status is None:
        return False
    return any(
        d.get("paired") and d.get("connected") and d.get("subscribed")
        for d in status.get("devices", [])
    )


def hci_events(hciconfig_output: str) -> Optional[int]:
    match = re.search(r"\bevents:(\d+)", hciconfig_output)
    return int(match.group(1)) if match else None


def iso(t: Optional[float]) -> str:
    if t is None:
        return "-"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the machine ------------------------------------------------------------------------------
class System:
    """Everything that touches D-Bus, systemd and the controller (replaced in the tests)."""

    def __init__(self) -> None:
        from gi.repository import Gio, GLib  # type: ignore # apt's python3-gi

        self.GLib = GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _call(self, name: str, path: str, interface: str, method: str, params: Any) -> Any:
        from gi.repository import Gio  # type: ignore

        return self.bus.call_sync(
            name, path, interface, method, params, None, Gio.DBusCallFlags.NONE,
            CALL_TIMEOUT_MS, None,
        ).unpack()

    def observer_status(self) -> Optional[Dict[str, Any]]:
        try:
            (text,) = self._call("ancs4linux.Observer", "/", "ancs4linux.Observer", "GetStatus", None)
            return json.loads(text)
        except Exception as e:  # not running, restarting, or no answer in time
            print(f"observer GetStatus failed: {e}")
            return None

    def adapter(self, prop: str, interface: str = "org.bluez.Adapter1") -> Any:
        try:
            (value,) = self._call(
                "org.bluez", ADAPTER, "org.freedesktop.DBus.Properties", "Get",
                self.GLib.Variant("(ss)", (interface, prop)),
            )
            return value
        except Exception as e:
            return f"? ({e})"

    def paired_connected(self) -> List[str]:
        """Paired devices bluetoothd says are connected."""
        try:
            (objects,) = self._call(
                "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", None
            )
        except Exception as e:
            print(f"GetManagedObjects failed: {e}")
            return []
        return [
            path for path, ifaces in objects.items()
            if ifaces.get("org.bluez.Device1", {}).get("Paired")
            and ifaces["org.bluez.Device1"].get("Connected")
        ]

    def events(self) -> Optional[int]:
        out = subprocess.run(["hciconfig", HCI], capture_output=True, text=True, timeout=10)
        return hci_events(out.stdout)

    def links(self) -> str:
        out = subprocess.run(["hcitool", "con"], capture_output=True, text=True, timeout=10)
        lines = [line.strip() for line in out.stdout.splitlines()[1:] if line.strip()]
        return "; ".join(lines) or "none"

    def systemctl(self, *args: str) -> None:
        subprocess.run(["systemctl", *args], check=True, timeout=90)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()


def snapshot(system: Any, label: str) -> None:
    before = system.events()
    system.sleep(10)
    after = system.events()
    rate = "?" if before is None or after is None else str(after - before)
    print(
        f"{label}: Discovering={system.adapter('Discovering')} "
        f"ActiveInstances={system.adapter('ActiveInstances', ADVERTISING)} hci_events_10s={rate} "
        f"links={system.links()}"
    )


def wait_for_phone(system: Any) -> Optional[int]:
    """Seconds until a paired phone connected, or None after WAIT_SECONDS."""
    start = system.now()
    while system.now() - start < WAIT_SECONDS:
        if system.paired_connected():
            return int(system.now() - start)
        system.sleep(POLL_SECONDS)
    return None


def attempt(system: Any, number: int) -> str:
    snapshot(system, f"attempt {number}/{MAX_ATTEMPTS} before")
    system.systemctl("stop", "ancs4linux-observer.service")
    try:
        system.sleep(3)  # bluetoothd ends the observer's discovery once its bus name is gone
        snapshot(system, "observer stopped")
        took = wait_for_phone(system)
    finally:
        system.systemctl("start", "ancs4linux-observer.service")
    if took is not None:
        return f"RESULT attempt {number}: phone connected {took} s after the scan stopped (stage 1, no bluetoothd restart)"
    system.systemctl("restart", "bluetooth.service")
    for unit in ("ancs4linux-observer", "ancs4linux-advertising", "ancs4linux-advertise"):
        system.systemctl("start", f"{unit}.service")  # a no-op if the restart brought it back
    system.sleep(3)
    snapshot(system, "bluetoothd restarted")
    took = wait_for_phone(system)
    if took is not None:
        return f"RESULT attempt {number}: phone connected {took} s after the bluetoothd restart (stage 2)"
    return f"RESULT attempt {number}: phone did not connect (away, or a restart isn't enough)"


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def run(system: Any, state_file: str = STATE_FILE, log: Callable[[str], None] = print) -> str:
    state = load_state(state_file)
    status = system.observer_status()
    if status is not None and not any(d.get("paired") for d in status.get("devices", [])):
        # No phone paired (e.g. removed for a re-pair): nothing to bring back.
        save_state(state_file, {})
        return "none"
    now = system.now()
    new, action = decide(state, live_from_status(status), now)
    save_state(state_file, new)
    if action == "recovered" and state.get("attempts"):
        log(f"phone back (down since {iso(state['down_since'])}, after {state['attempts']} attempt(s))")
    elif action == "recovered":
        log(f"phone back (down since {iso(state['down_since'])}, no attempt needed)")
    elif not state and new:
        log("phone not connected and subscribed; counting from now")
    elif action in ("attempt", "gave_up"):
        n = new["attempts"]
        log(f"phone down since {iso(new['down_since'])}; attempt {n}/{MAX_ATTEMPTS}")
        log(attempt(system, n))
        if action == "gave_up":
            log(f"that was the last attempt; waiting for the phone to come back on its own")
    return action


def check(system: Any) -> None:
    """--check: what a tick would see, changing nothing (for testing the install by hand)."""
    status = system.observer_status()
    print(f"observer answers: {status is not None}; live: {live_from_status(status)}")
    print(f"paired and connected (bluetoothd): {system.paired_connected() or 'none'}")
    snapshot(system, "now")
    print(f"state: {load_state(STATE_FILE) or 'none (phone live, or not yet seen down)'}")


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        check(System())
    else:
        run(System())
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    sys.exit(main())
