import logging
from typing import Any, Dict, List, Optional

from gi.repository import GLib  # type: ignore # dynamic

from ancs4linux.common.apis import ObserverAPI
from ancs4linux.observer import bluez
from ancs4linux.observer.ancs.constants import (
    ANCS_CHARS,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
)
from ancs4linux.observer.device import MobileDevice, now_iso
from ancs4linux.observer.device_comm import GATT_CHARACTERISTIC

log = logging.getLogger(__name__)

DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"
SCAN_RECHECK_SECONDS = 60


def device_of(char_path: str) -> str:
    # /org/bluez/hci0/dev_XX/serviceNNNN/charNNNN -> /org/bluez/hci0/dev_XX
    return "/".join(char_path.split("/")[:-2])


class Scanner:
    """Tracks paired phones and their ANCS characteristics from bluetoothd's objects.

    Uses a fixed set of bus-wide subscriptions (see bluez.py) rather than a proxy per
    object: with discovery running, bluetoothd lists dozens of passing devices a minute.
    """

    def __init__(self, server: ObserverAPI):
        self.server = server
        self.devices: Dict[str, MobileDevice] = {}
        # LE discovery while a paired phone is disconnected (added on fe-pro): hearing the
        # phone's own advertisements tells consumers it's nearby even though it isn't
        # connecting. Stopped while connected, so it never competes with the link.
        self.scanning = False
        self.scanning_since: Optional[str] = None
        self.scan_call_pending = False
        self.adapter_path: Optional[str] = None

    def start_observing(self) -> None:
        bluez.on_object_manager(self.process_object, self.remove_object)
        bluez.on_properties_changed(DEVICE, self.device_changed)
        bluez.on_properties_changed(GATT_CHARACTERISTIC, self.char_changed)
        objects = bluez.managed_objects()
        # Devices first: a characteristic is only taken for a device already known.
        for path, interfaces in sorted(objects.items(), key=lambda o: DEVICE not in o[1]):
            self.process_object(path, interfaces)
        self.update_scanning()
        GLib.timeout_add_seconds(SCAN_RECHECK_SECONDS, self.recheck_scanning)

    # --- objects -------------------------------------------------------------------
    def process_object(self, path: str, interfaces: Dict[str, Dict[str, Any]]) -> None:
        if ADAPTER in interfaces and self.adapter_path is None:
            self.adapter_path = path
        if DEVICE in interfaces:
            self.device_changed(path, interfaces[DEVICE], [])
        if GATT_CHARACTERISTIC in interfaces:
            uuid = interfaces[GATT_CHARACTERISTIC].get("UUID")
            device = self.devices.get(device_of(path))
            if uuid in ANCS_CHARS and device is not None:
                if uuid == NOTIFICATION_SOURCE_CHAR:
                    device.set_notification_source(path)
                elif uuid == CONTROL_POINT_CHAR:
                    device.set_control_point(path)
                elif uuid == DATA_SOURCE_CHAR:
                    device.set_data_source(path)

    def remove_object(self, path: str, interfaces: List[str]) -> None:
        # Only forget the device when Device1 itself goes away. Other interfaces on the
        # device path (e.g. Battery1) disappear on every disconnect.
        if DEVICE in interfaces and path in self.devices:
            device = self.devices.pop(path)
            device.unsubscribe()
            log.info(f"{path} removed from bluetoothd")
            self.update_scanning()
        elif GATT_CHARACTERISTIC in interfaces:
            device = self.devices.get(device_of(path))
            if device is not None:
                device.char_removed(path)

    def device_changed(self, path: str, changes: Dict[str, Any], _: List[str]) -> None:
        # Only paired devices can be ANCS phones. Discovery makes bluetoothd list every
        # advertiser nearby (other people's phones, TVs, ...); ignore those.
        device = self.devices.get(path)
        if device is None:
            if not changes.get("Paired"):
                return
            device = self.devices[path] = MobileDevice(path, self.server)
            # Newly paired (or first seen): changes may not include its name/connection
            # state, so read everything once.
            bluez.get_all(path, DEVICE, lambda props, err: self.got_all(path, props, err))
        if "Paired" in changes:
            device.set_paired(changes["Paired"])
        if "Connected" in changes:
            device.set_connected(changes["Connected"])
        if "Alias" in changes:
            device.set_name(changes["Alias"])
        if "RSSI" in changes and changes["RSSI"] != 0:
            # 0 = bluetoothd invalidating it when discovery stops
            device.note_seen(changes["RSSI"])
        if "Paired" in changes or "Connected" in changes:
            self.update_scanning()

    def got_all(self, path: str, props: Any, error: Optional[str]) -> None:
        if error is not None:
            log.warning(f"Reading {path} failed: {error}")
            return
        if path in self.devices:
            # Only what's missing, so a fresher change isn't overwritten by older state.
            device = self.devices[path]
            if device.name is None and "Alias" in props:
                device.set_name(props["Alias"])
            if not device.connection_observed and "Connected" in props:
                device.set_connected(props["Connected"])
                self.update_scanning()

    def char_changed(self, path: str, changes: Dict[str, Any], _: List[str]) -> None:
        if "Value" not in changes:
            return
        device = self.devices.get(device_of(path))
        if device is not None:
            device.on_char_value(path, changes["Value"])

    # --- discovery -----------------------------------------------------------------
    def want_scanning(self) -> bool:
        return any(d.paired and not d.connected for d in self.devices.values())

    def adapter_call(self, interface: str, method: str, params: Any = None, done=None) -> None:
        def finish(result: Any, error: Optional[str]) -> None:
            self.scan_call_pending = False
            if error is not None:
                log.warning(f"{method} failed: {error}")
            elif done:
                done(result)
            # Things may have changed while the call was in flight.
            self.update_scanning()

        self.scan_call_pending = True
        bluez.call(self.adapter_path, interface, method, params, finish)

    def update_scanning(self) -> None:
        if self.adapter_path is None or self.scan_call_pending:
            return
        want = self.want_scanning()
        if want and not self.scanning:
            self.adapter_call(
                ADAPTER,
                "SetDiscoveryFilter",
                GLib.Variant(
                    "(a{sv})",
                    (
                        {
                            "Transport": GLib.Variant("s", "le"),
                            # Report every advertisement, so last_seen stays current. (A
                            # filter also stops bluetoothd from hiding RSSI changes < 8 dB.)
                            "DuplicateData": GLib.Variant("b", True),
                        },
                    ),
                ),
                done=lambda _: self.start_discovery(),
            )
        elif not want and self.scanning:
            self.adapter_call(ADAPTER, "StopDiscovery", done=self.scan_stopped)

    def start_discovery(self) -> None:
        # Called from SetDiscoveryFilter's finish(), before it clears scan_call_pending.
        def started(_: Any, error: Optional[str]) -> None:
            self.scan_call_pending = False
            if error is not None:
                log.warning(f"StartDiscovery failed: {error}")
            else:
                self.scan_started()
            self.update_scanning()

        self.scan_call_pending = True
        bluez.call(self.adapter_path, ADAPTER, "StartDiscovery", None, started)

    def scan_started(self) -> None:
        self.scanning = True
        self.scanning_since = now_iso()
        log.info("Scanning for paired phones (none connected)")
        for d in self.devices.values():
            if d.paired and not d.connected:
                d.note_scan_started()

    def scan_stopped(self, _: Any) -> None:
        self.scanning = False
        self.scanning_since = None
        log.info("Stopped scanning (paired phone connected)")
        for d in self.devices.values():
            d.note("scan_stopped")

    def recheck_scanning(self) -> bool:
        # bluetoothd ends a client's discovery if it restarts; notice and start again.
        if self.scanning and self.adapter_path and not self.scan_call_pending:
            def check(discovering: Any) -> None:
                if not discovering:
                    log.warning("Discovery stopped behind our back; starting again")
                    self.scanning = False

            self.adapter_call(
                "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (ADAPTER, "Discovering")), done=check,
            )
        else:
            self.update_scanning()
        return True
