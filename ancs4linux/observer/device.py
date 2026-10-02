import logging
from typing import Optional

from ancs4linux.common.apis import ObserverAPI
from ancs4linux.common.dbus import InvalidAction, ObjPath, get_dbus_error_name
from ancs4linux.common.external_apis import BluezGattCharacteristicAPI
from ancs4linux.common.task_restarter import TaskRestarter
from ancs4linux.observer.device_comm import DeviceCommunicator

log = logging.getLogger(__name__)


class MobileDevice:
    def __init__(self, path: str, server: ObserverAPI):
        self.server = server
        self.path = path
        self.communicator: Optional[DeviceCommunicator] = None
        # Bumped by every subscribe attempt; an attempt that finds it has been superseded
        # stops without attaching, so only the newest one ends up subscribed.
        self.generation = 0

        self.paired = False
        self.connected = False
        self.name: Optional[str] = None
        self.notification_source: Optional[BluezGattCharacteristicAPI] = None
        self.control_point: Optional[BluezGattCharacteristicAPI] = None
        self.data_source: Optional[BluezGattCharacteristicAPI] = None

    def set_notification_source(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.notification_source = BluezGattCharacteristicAPI.connect(path)
        self.try_subscribe()

    def set_control_point(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.control_point = BluezGattCharacteristicAPI.connect(path)
        self.try_subscribe()

    def set_data_source(self, path: ObjPath) -> None:
        self.unsubscribe()
        self.data_source = BluezGattCharacteristicAPI.connect(path)
        self.try_subscribe()

    def set_paired(self, paired: bool) -> None:
        self.unsubscribe()
        self.paired = paired
        self.try_subscribe()

    def set_connected(self, connected: bool) -> None:
        self.unsubscribe()
        self.connected = connected
        self.try_subscribe()

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
        generation = self.generation
        log.info(f"Asking for notifications (attempt {generation})...")
        TaskRestarter(
            120,
            1,
            lambda: self.try_asking(generation),
            lambda: log.info("Asking for notifications: success."),
            lambda: log.error("Failed to subscribe to notifications."),
        ).try_running_bg()

    def try_asking(self, generation: int) -> bool:
        if generation != self.generation or self.communicator is not None:
            log.debug(f"Subscribe attempt {generation} superseded.")
            return True
        assert self.notification_source and self.control_point and self.data_source
        try:
            # FIXME: blocking here (e.g. due to device not responding) can lock our program.
            # Timeouts (timeout=1000 [ms]) do not work.
            self.data_source.StartNotify()
            self.notification_source.StartNotify()
        except Exception as e:
            log.warn(
                f"Failed to start subscribe to notifications (is phone paired?): {e}"
            )
            if get_dbus_error_name(e) is not None:
                log.warn(f"Original error: {get_dbus_error_name(e)}")
            return False

        comm = DeviceCommunicator(self)
        comm.attach()
        self.communicator = comm

        return True

    def handle_action(self, notification_id: int, is_positive: bool) -> None:
        if self.communicator is None:
            raise InvalidAction("Phone is not connected/subscribed")
        self.communicator.ask_for_action(notification_id, is_positive)
