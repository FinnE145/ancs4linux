import json
import logging
import random
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from gi.repository import GLib  # type: ignore # dynamic

from ancs4linux.common.apis import ObserverAPI
from ancs4linux.common.dbus import InvalidAction, ObjPath
from ancs4linux.common.apis import ShowNotificationData
from ancs4linux.observer import bluez
from ancs4linux.observer.device_comm import GATT_CHARACTERISTIC, DeviceCommunicator

log = logging.getLogger(__name__)

HISTORY_LENGTH = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


OBSERVER_STARTED = now_iso()

CALL_TIMEOUT_MS = bluez.CALL_TIMEOUT_MS
SUBSCRIBE_RETRY_SECONDS = 2
# Minimum gap between a StopNotify on the Notification Source and the next StartNotify,
# whichever subscribe attempt sends them. bluetoothd replies to StopNotify at once and writes
# the "disable" to the phone afterwards; re-enabling while that write is pending hits a
# use-after-free in BlueZ's gatt-client (fixed upstream after 5.87, "Fix calling destroy
# after unregistering notify"), and the corrupted heap crashed bluetoothd on the next
# disconnect.
STOP_START_GAP_SECONDS = 1.5
SUBSCRIBE_MAX_ATTEMPTS = 60


class MobileDevice:
    def __init__(self, path: str, server: ObserverAPI):
        self.server = server
        self.path = path
        self.communicator: Optional[DeviceCommunicator] = None
        # Bumped by every subscribe attempt; an attempt that finds it has been superseded
        # stops without attaching, so only the newest one ends up subscribed.
        self.generation = 0
        # Ids are id_base + iOS notification UID. iOS keeps UIDs across reconnects, so the
        # base lives as long as the device (upstream picked a new one per connection,
        # which gave the same notification a different id after every reconnect).
        self.id_base = random.randint(1, 10**5) * 1000
        # Ids that still exist on the phone, with the actions iOS offers for each:
        # {id: (positive, negative)}. Reset on each fresh subscription and rebuilt from
        # the full list iOS then sends.
        self.actionable: Dict[int, Tuple[bool, bool]] = dict()
        # Facts for GetStatus() (UTC ISO timestamps; None = not known, e.g. the phone was
        # already connected when the observer started).
        self.connected_since: Optional[str] = None
        self.last_disconnected: Optional[str] = None
        self.subscribed_since: Optional[str] = None
        self.shown_since_subscribed = 0
        self.last_notification: Optional[str] = None
        self.subscribe_failures = 0  # on the current connection
        self.last_subscribe_error: Optional[str] = None
        self.history: deque = deque(maxlen=HISTORY_LENGTH)
        self.connection_observed = False
        # Heard the phone's own advertisements (only while scanning, i.e. disconnected).
        self.last_seen: Optional[str] = None
        self.last_rssi: Optional[int] = None
        self.seen_since_scan_started = False

        self.paired = False
        self.connected = False
        self.name: Optional[str] = None
        # Object paths of the phone's ANCS characteristics (no proxies: see bluez.py).
        self.notification_source: Optional[str] = None
        self.control_point: Optional[str] = None
        self.data_source: Optional[str] = None
        self.last_stop_notify = 0.0  # time.monotonic() of the last NS StopNotify sent
        # The current list as last reported (for GetNotifications): ShowNotification JSON
        # by id, reset on each fresh subscription, kept across a disconnect.
        self.current: Dict[int, Dict[str, Any]] = {}
        self.current_updated: Optional[str] = None

    def set_notification_source(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.notification_source = path
        self.try_subscribe()

    def set_control_point(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.control_point = path
        self.try_subscribe()

    def set_data_source(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.data_source = path
        self.try_subscribe()

    def char_removed(self, path: str) -> None:
        if path in (self.notification_source, self.control_point, self.data_source):
            self.unsubscribe()
            if path == self.notification_source:
                self.notification_source = None
            if path == self.control_point:
                self.control_point = None
            if path == self.data_source:
                self.data_source = None

    def on_char_value(self, path: str, value: List[int]) -> None:
        comm = self.communicator
        if comm is None:
            return
        if path == self.notification_source:
            comm.on_ns_value(value)
        elif path == self.data_source:
            comm.on_ds_value(value)

    def set_paired(self, paired: bool) -> None:
        self.unsubscribe()
        self.paired = paired
        self.try_subscribe()

    def set_connected(self, connected: bool) -> None:
        self.unsubscribe()
        if not self.connection_observed:
            # First report after the observer started: when it connected isn't known.
            self.connection_observed = True
            if connected:
                self.note("already_connected")
                self.server.emit_connected(self.path)
        elif connected != self.connected:
            self.record_connection(connected)
        self.connected = connected
        self.try_subscribe()

    def record_connection(self, connected: bool) -> None:
        if connected:
            self.connected_since = now_iso()
            self.subscribe_failures = 0
            self.last_subscribe_error = None
            self.note("connected")
            self.server.emit_connected(self.path)
        else:
            self.last_disconnected = now_iso()
            self.connected_since = None
            self.subscribed_since = None
            self.shown_since_subscribed = 0
            self.note("disconnected")
            self.server.emit_disconnected(self.path)

    def note(self, event: str, **fields: Any) -> None:
        self.history.append({"ts": now_iso(), "event": event, **fields})

    def note_scan_started(self) -> None:
        self.seen_since_scan_started = False
        self.note("scan_started")

    def note_seen(self, rssi: int) -> None:
        self.last_seen = now_iso()
        self.last_rssi = rssi
        if not self.seen_since_scan_started:
            self.seen_since_scan_started = True
            self.note("seen", rssi=rssi)

    def note_shown(self, data: ShowNotificationData) -> None:
        self.shown_since_subscribed += 1
        self.last_notification = now_iso()
        self.current[data.id] = json.loads(data.json())
        self.current_updated = self.last_notification

    def note_dismissed(self, id: int) -> None:
        if self.current.pop(id, None) is not None:
            self.current_updated = now_iso()

    def notifications(self) -> Dict[str, Any]:
        ids = sorted(self.current)
        return {
            "device": self.path,
            "name": self.name,
            "connected": self.connected,
            "subscribed": self.communicator is not None,
            "subscribed_since": self.subscribed_since,
            "updated": self.current_updated,
            # ids are id_base + iOS UID; id_base changes only when the observer restarts
            # (then every id changes, and a Subscribed + full list follows).
            "id_base": self.id_base,
            "id_min": ids[0] if ids else None,
            "id_max": ids[-1] if ids else None,
            "notifications": [self.current[i] for i in ids],
        }

    def status(self) -> Dict[str, Any]:
        return {
            "device": self.path,
            "name": self.name,
            "paired": self.paired,
            "connected": self.connected,
            "connected_since": self.connected_since,
            "last_disconnected": self.last_disconnected,
            "subscribed": self.communicator is not None,
            "subscribed_since": self.subscribed_since,
            "shown_since_subscribed": self.shown_since_subscribed,
            "last_notification": self.last_notification,
            "subscribe_failures": self.subscribe_failures,
            "last_subscribe_error": self.last_subscribe_error,
            "last_seen": self.last_seen,
            "last_rssi": self.last_rssi,
            "history": list(self.history),
        }

    def set_name(self, name: str) -> None:
        # No self.unsubscribe(): name change is innocent
        self.name = name
        self.try_subscribe()

    def unsubscribe(self) -> None:
        if self.communicator is not None:
            self.communicator.detach()
        self.communicator = None

    def try_subscribe(self) -> None:
        log.debug(
            f"{self.path}: {self.paired} {self.connected} {not self.communicator}"
        )
        if not (
            self.paired
            and self.connected
            and self.name
            and self.notification_source
            and self.control_point
            and self.data_source
            and not self.communicator
        ):
            return

        self.generation += 1
        self.start_subscribe(self.generation, 1)

    # Subscribing is a chain of asynchronous calls, each with a time limit. Upstream made
    # them synchronously with dasbus's default timeout, which is infinite: when bluetoothd
    # never answered one (seen after a link dropped mid-request), the whole observer froze
    # for days.
    def subscribe_steps(self) -> List[Tuple[Any, str]]:
        return [
            (self.data_source, "StartNotify"),
            (self.notification_source, "StartNotify"),
            # iOS only sends its full Notification Center list when the subscription is
            # switched on. After a reconnect BlueZ can restore the old subscription, and
            # then iOS sends nothing: notifications that arrived or were cleared during
            # the gap would be missed. Switching it off and on forces the full list.
            # (Any NS StartNotify waits STOP_START_GAP_SECONDS after the last StopNotify.)
            (self.notification_source, "StopNotify"),
            (self.notification_source, "StartNotify"),
        ]

    def subscribe_is_current(self, generation: int) -> bool:
        return (
            generation == self.generation
            and self.communicator is None
            and self.connected
        )

    def start_subscribe(self, generation: int, attempt: int) -> bool:
        if not self.subscribe_is_current(generation):
            log.debug(f"Subscribe attempt {generation} superseded.")
            return False
        log.info(f"Asking for notifications (attempt {generation}.{attempt})...")
        self.run_subscribe_steps(generation, attempt, self.subscribe_steps())
        return False  # also used as a one-shot GLib timeout callback

    def run_subscribe_steps(
        self, generation: int, attempt: int, steps: List[Tuple[Any, str]]
    ) -> None:
        if not self.subscribe_is_current(generation):
            log.debug(f"Subscribe attempt {generation} superseded.")
            return
        if not steps:
            self.finish_subscribe(generation)
            return
        path, method = steps[0]
        if path is None:
            self.subscribe_failed(generation, attempt, f"{method}: characteristic gone")
            return

        if path == self.notification_source and method == "StartNotify":
            wait = self.last_stop_notify + STOP_START_GAP_SECONDS - time.monotonic()
            if wait > 0:
                GLib.timeout_add(
                    int(wait * 1000) + 1,
                    lambda: self.run_subscribe_steps(generation, attempt, steps) and False,
                )
                return
        if path == self.notification_source and method == "StopNotify":
            self.last_stop_notify = time.monotonic()

        def done(_: Any, error: Optional[str]) -> None:
            if error is not None:
                self.subscribe_failed(generation, attempt, f"{method}: {error}")
                return
            self.run_subscribe_steps(generation, attempt, steps[1:])

        bluez.call(path, GATT_CHARACTERISTIC, method, None, done)

    def subscribe_failed(self, generation: int, attempt: int, error: str) -> None:
        if not self.subscribe_is_current(generation):
            return
        log.warning(f"Subscribe attempt {generation}.{attempt} failed: {error}")
        self.subscribe_failures += 1
        self.last_subscribe_error = error
        if attempt >= SUBSCRIBE_MAX_ATTEMPTS:
            log.error("Failed to subscribe to notifications; waiting for a reconnect.")
            self.note("subscribe_failed", error=error)
            self.server.emit_subscribe_failed(self.path, error)
            return
        GLib.timeout_add_seconds(
            SUBSCRIBE_RETRY_SECONDS, self.start_subscribe, generation, attempt + 1
        )

    def finish_subscribe(self, generation: int) -> None:
        self.communicator = DeviceCommunicator(self)
        self.actionable.clear()
        self.current.clear()
        self.current_updated = now_iso()
        self.subscribed_since = now_iso()
        self.shown_since_subscribed = 0
        self.note("subscribed")
        log.info(f"Asking for notifications: success (attempt {generation}).")
        self.server.emit_subscribed(self.path)

    def handle_action(self, notification_id: int, is_positive: bool) -> None:
        if self.communicator is None:
            raise InvalidAction("Phone is not connected/subscribed")
        self.communicator.ask_for_action(notification_id, is_positive)
