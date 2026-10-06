import logging
from functools import partial
from typing import Any, Dict, List, Optional

from gi.repository import GLib  # type: ignore # dynamic

from ancs4linux.common.apis import ObserverAPI
from ancs4linux.common.dbus import ObjPath, Str, SystemBus, Variant
from ancs4linux.common.external_apis import (
    BluezDeviceAPI,
    BluezGattCharacteristicAPI,
    BluezRootAPI,
)
from ancs4linux.observer.ancs.constants import (
    ANCS_CHARS,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
    NOTIFICATION_SOURCE_CHAR,
)
from ancs4linux.observer.device import CALL_TIMEOUT_MS, MobileDevice, now_iso

log = logging.getLogger(__name__)

SCAN_RECHECK_SECONDS = 60


class Scanner:
    def __init__(self, server: ObserverAPI):
        self.server = server
        self.root = BluezRootAPI.connect()
        self.devices: Dict[str, MobileDevice] = {}
        self.property_observers: Dict[str, BluezDeviceAPI] = {}
        # LE discovery while a paired phone is disconnected (added on fe-pro): hearing the
        # phone's own advertisements tells consumers it's nearby even though it isn't
        # connecting. Stopped while connected, so it never competes with the link.
        self.scanning = False
        self.scanning_since: Optional[str] = None
        self.scan_call_pending = False
        self.adapter_path: Optional[str] = None

    def start_observing(self):
        self.root.InterfacesAdded.connect(self.process_object)
        self.root.InterfacesRemoved.connect(self.remove_observers)
        for path, services in self.root.GetManagedObjects().items():
            if "org.bluez.Adapter1" in services and self.adapter_path is None:
                self.adapter_path = path
            self.process_object(path, services)
        self.update_scanning()
        GLib.timeout_add_seconds(SCAN_RECHECK_SECONDS, self.recheck_scanning)

    def process_object(
        self, path: ObjPath, services: Dict[Str, Dict[Str, Variant]]
    ) -> None:
        if BluezDeviceAPI.interface in services:
            if path not in self.property_observers:
                self.property_observers[path] = BluezDeviceAPI.connect(path)
                self.property_observers[path].PropertiesChanged.connect(
                    partial(self.process_property, path)
                )
                self.process_property(
                    path,
                    BluezDeviceAPI.interface,
                    services[BluezDeviceAPI.interface],
                    [],
                )

        if BluezGattCharacteristicAPI.interface in services:
            uuid = services[BluezGattCharacteristicAPI.interface]["UUID"].unpack()
            if uuid in ANCS_CHARS:
                device = "/".join(path.split("/")[:-2])
                self.devices.setdefault(device, MobileDevice(device, self.server))
                if uuid == NOTIFICATION_SOURCE_CHAR:
                    self.devices[device].set_notification_source(path)
                elif uuid == CONTROL_POINT_CHAR:
                    self.devices[device].set_control_point(path)
                elif uuid == DATA_SOURCE_CHAR:
                    self.devices[device].set_data_source(path)

    def process_property(
        self,
        device: ObjPath,
        interface: str,
        changes: Dict[str, Variant],
        invalidated: List[str],
    ) -> None:
        if interface == BluezDeviceAPI.interface:
            # Only paired devices can be ANCS phones. Discovery makes bluetoothd list every
            # advertiser nearby (other people's phones, TVs, ...); ignore those.
            if device not in self.devices:
                if not ("Paired" in changes and changes["Paired"].unpack()):
                    return
                self.devices[device] = MobileDevice(device, self.server)
            if "Paired" in changes:
                self.devices[device].set_paired(changes["Paired"].unpack())
            if "Connected" in changes:
                self.devices[device].set_connected(changes["Connected"].unpack())
            if "Alias" in changes:
                self.devices[device].set_name(changes["Alias"].unpack())
            if "RSSI" in changes:
                rssi = changes["RSSI"].unpack()
                if rssi != 0:  # 0 = bluetoothd invalidating it when discovery stops
                    self.devices[device].note_seen(rssi)
            if "Paired" in changes or "Connected" in changes:
                self.update_scanning()
            return

    # --- discovery ----------------------------------------------------------------
    def want_scanning(self) -> bool:
        return any(d.paired and not d.connected for d in self.devices.values())

    def adapter_call(self, interface: str, method: str, *args: Any, done=None) -> None:
        proxy = SystemBus().get_proxy(
            "org.bluez", self.adapter_path, interface_name=interface
        )

        def finish(call: Any) -> None:
            self.scan_call_pending = False
            try:
                result = call()
            except Exception as e:
                log.warning(f"{method} failed: {e}")
                return
            if done:
                done(result)

        self.scan_call_pending = True
        try:
            getattr(proxy, method)(*args, callback=finish, timeout=CALL_TIMEOUT_MS)
        except Exception as e:
            self.scan_call_pending = False
            log.warning(f"{method} failed: {e}")

    def update_scanning(self) -> None:
        if self.adapter_path is None or self.scan_call_pending:
            return
        want = self.want_scanning()
        if want and not self.scanning:
            self.adapter_call(
                "org.bluez.Adapter1",
                "SetDiscoveryFilter",
                {
                    "Transport": Variant("s", "le"),
                    # Report every advertisement, so last_seen stays current. (A filter
                    # also stops bluetoothd from hiding RSSI changes smaller than 8 dB.)
                    "DuplicateData": Variant("b", True),
                },
                done=lambda _: self.adapter_call(
                    "org.bluez.Adapter1", "StartDiscovery", done=self.scan_started
                ),
            )
        elif not want and self.scanning:
            self.adapter_call("org.bluez.Adapter1", "StopDiscovery", done=self.scan_stopped)

    def scan_started(self, _: Any) -> None:
        self.scanning = True
        self.scanning_since = now_iso()
        log.info("Scanning for paired phones (none connected)")
        for d in self.devices.values():
            if d.paired and not d.connected:
                d.note_scan_started()
        # The phone may have connected while StartDiscovery was in flight.
        self.update_scanning()

    def scan_stopped(self, _: Any) -> None:
        self.scanning = False
        self.scanning_since = None
        log.info("Stopped scanning (paired phone connected)")
        for d in self.devices.values():
            d.note("scan_stopped")
        self.update_scanning()

    def recheck_scanning(self) -> bool:
        # bluetoothd ends a client's discovery if it restarts; notice and start again.
        if self.scanning and self.adapter_path:
            def check(discovering: Any) -> None:
                value = discovering.unpack() if hasattr(discovering, "unpack") else discovering
                if not value:
                    log.warning("Discovery stopped behind our back; starting again")
                    self.scanning = False
                    self.update_scanning()

            self.adapter_call(
                "org.freedesktop.DBus.Properties", "Get", "org.bluez.Adapter1",
                "Discovering", done=check,
            )
        else:
            self.update_scanning()
        return True

    def remove_observers(self, path: ObjPath, services: List[Str]) -> None:
        # Only forget the device when Device1 itself goes away. Other interfaces on the
        # device path (e.g. Battery1) disappear on every disconnect, and dropping the
        # watcher then means a reconnect is never noticed.
        if BluezDeviceAPI.interface not in services:
            return
        if path in self.property_observers:
            self.property_observers[path].PropertiesChanged.disconnect()
            del self.property_observers[path]
