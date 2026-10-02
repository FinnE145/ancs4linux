import random
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ancs4linux.common.apis import ShowNotificationData
from ancs4linux.common.dbus import Variant
from ancs4linux.observer.ancs.builders import (
    GetAppAttributes,
    GetNotificationAttributes,
    PerformNotificationAction,
)
from ancs4linux.observer.ancs.constants import (
    UINT_MAX,
    CategoryID,
    CommandID,
    EventID,
)
from ancs4linux.observer.ancs.parsers import (
    AppAttributes,
    DataSourceEvent,
    Notification,
    NotificationAttributes,
)

CATEGORY_NAMES = {
    value: name
    for name, value in vars(CategoryID).items()
    if not name.startswith("_") and isinstance(value, int)
}

EVENT_NAMES = {
    EventID.NotificationAdded: "added",
    EventID.NotificationModified: "modified",
}

if TYPE_CHECKING:
    from ancs4linux.observer.device import MobileDevice


class DeviceCommunicator:
    def __init__(self, device: "MobileDevice"):
        self.device = device
        self.id = random.randint(1, 10**5) * 1000
        self.notification_queue: List[ShowNotificationData] = []
        self.awaiting_app_names: Set[str] = set()
        self.known_app_names: Dict[str, str] = dict()
        # Notification Source events awaiting their attributes, by iOS notification UID.
        self.pending_events: Dict[int, Notification] = dict()

    def attach(self) -> None:
        assert self.device.notification_source and self.device.data_source
        # Keep the proxies we attached to: the device may swap in new ones on reconnect,
        # and detach() must remove our handlers from these, not from the new ones.
        self.notification_source = self.device.notification_source
        self.data_source = self.device.data_source
        self.notification_source.PropertiesChanged.disconnect()
        self.notification_source.PropertiesChanged.connect(self.on_ns_change)
        self.data_source.PropertiesChanged.disconnect()
        self.data_source.PropertiesChanged.connect(self.on_ds_change)

    def detach(self) -> None:
        self.notification_source.PropertiesChanged.disconnect(self.on_ns_change)
        self.data_source.PropertiesChanged.disconnect(self.on_ds_change)

    def on_ns_change(
        self, interface: str, changes: Dict[str, Variant], invalidated: List[str]
    ) -> None:
        if interface != "org.bluez.GattCharacteristic1" or "Value" not in changes:
            return

        notification = Notification.parse(changes["Value"].unpack())
        if notification.type in (
            EventID.NotificationAdded,
            EventID.NotificationModified,
        ):
            # Upstream turned pre-existing additions into dismissals; fetch them all and
            # pass the flag on instead, so consumers decide what "old" means.
            self.pending_events[notification.id] = notification
            self.ask_for_notification_details(notification)
        else:
            self.pending_events.pop(notification.id, None)
            self.device.server.emit_dismiss_notification(self.host_id(notification.id))

    def host_id(self, uid: int) -> int:
        return (self.id + uid) % UINT_MAX

    @staticmethod
    def event_fields(event: Optional[Notification]) -> Dict[str, Any]:
        if event is None:  # attributes arrived without a matching event: unknown
            return {}
        return {
            "event": EVENT_NAMES.get(event.type, str(event.type)),
            "pre_existing": event.is_preexisting(),
            "silent": event.is_silent(),
            "important": event.is_important(),
            "category": CATEGORY_NAMES.get(event.category, str(event.category)),
            "category_count": event.category_count,
        }

    def ask_for_notification_details(self, notification: Notification) -> None:
        msg = GetNotificationAttributes(
            id=notification.id,
            get_positive_action=notification.has_positive_action(),
            get_negative_action=notification.has_negative_action(),
        )
        assert self.device.control_point
        self.device.control_point.WriteValue(msg.to_list(), {})

    def on_ds_change(
        self, interface: str, changes: Dict[str, Variant], invalidated: List[str]
    ) -> None:
        if interface != "org.bluez.GattCharacteristic1" or "Value" not in changes:
            return

        ev = DataSourceEvent.parse(changes["Value"].unpack())
        if ev.type == CommandID.GetNotificationAttributes:
            self.on_notification_attributes(ev.as_notification_attributes())
        elif ev.type == CommandID.GetAppAttributes:
            self.on_app_attributes(ev.as_app_attributes())

    def on_notification_attributes(self, attrs: NotificationAttributes) -> None:
        assert self.device.name
        event = self.pending_events.pop(attrs.id, None)
        self.queue_notification(
            ShowNotificationData(
                device_handle=self.device.path,
                device_name=self.device.name,
                app_id=attrs.app_id,
                app_name="",
                id=self.host_id(attrs.id),
                title=attrs.title,
                body=attrs.message,
                positive_action=attrs.positive_action,
                negative_action=attrs.negative_action,
                subtitle=attrs.subtitle,
                date=attrs.date,
                message_size=attrs.message_size,
                **self.event_fields(event),
            )
        )
        self.process_queue()

    def on_app_attributes(self, attrs: AppAttributes) -> None:
        self.known_app_names[attrs.app_id] = attrs.app_name
        if attrs.app_id in self.awaiting_app_names:
            self.awaiting_app_names.remove(attrs.app_id)
        self.process_queue()

    def queue_notification(self, data: ShowNotificationData) -> None:
        self.notification_queue.append(data)

    def ask_for_app_name(self, app_id: str) -> None:
        self.awaiting_app_names.add(app_id)
        msg = GetAppAttributes(app_id=app_id)
        assert self.device.control_point
        self.device.control_point.WriteValue(msg.to_list(), {})

    def process_queue(self) -> None:
        unprocessed = []
        for data in self.notification_queue:
            if data.app_name != "":
                self.device.server.emit_show_notification(data)
            elif data.app_id in self.known_app_names:
                data.app_name = self.known_app_names[data.app_id]
                self.device.server.emit_show_notification(data)
            elif data.app_id in self.awaiting_app_names:
                unprocessed.append(data)
            else:
                self.ask_for_app_name(data.app_id)
                unprocessed.append(data)
        self.notification_queue = unprocessed

    def ask_for_action(self, notification_id: int, is_positive: bool) -> None:
        id = (notification_id - self.id) % UINT_MAX
        msg = PerformNotificationAction(notification_id=id, is_positive=is_positive)
        assert self.device.control_point
        self.device.control_point.WriteValue(msg.to_list(), {})
