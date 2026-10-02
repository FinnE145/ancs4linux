from ancs4linux.common.apis import ObserverAPI
from ancs4linux.common.dbus import (
    Bool,
    InvalidAction,
    Str,
    UInt32,
    dbus_interface,
    dbus_signal,
)
from ancs4linux.observer.scanner import Scanner


@dbus_interface(ObserverAPI.interface)
class ObserverServer(ObserverAPI):
    def set_scanner(self, scanner: Scanner) -> None:
        self.scanner = scanner

    def InvokeDeviceAction(
        self, device_handle: Str, notification_id: UInt32, is_positive: Bool
    ) -> None:
        # Upstream ignored bad requests silently; report them so the caller knows the
        # action did not happen.
        if self.scanner is None or device_handle not in self.scanner.devices:
            raise InvalidAction(f"Unknown device {device_handle}")
        self.scanner.devices[device_handle].handle_action(notification_id, is_positive)

    @dbus_signal
    def ShowNotification(self, json: Str) -> None:
        pass

    @dbus_signal
    def DismissNotification(self, id: UInt32) -> None:
        pass

    @dbus_signal
    def Subscribed(self, device_handle: Str) -> None:
        pass
