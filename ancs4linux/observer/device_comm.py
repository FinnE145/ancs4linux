import logging
import struct
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from gi.repository import GLib  # type: ignore # dynamic

from ancs4linux.common.apis import ShowNotificationData
from ancs4linux.common.dbus import InvalidAction
from ancs4linux.observer import bluez
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
    NotificationAttributeID,
)
from ancs4linux.observer.ancs.parsers import (
    AppAttributes,
    Notification,
    NotificationAttributes,
)

log = logging.getLogger(__name__)

GATT_CHARACTERISTIC = "org.bluez.GattCharacteristic1"
# A Data Source response can span several notifications; if the rest never comes (link
# dropped, iOS gave up), give up on the partial one after this long.
DATA_SOURCE_STALL_MS = 3000

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
        self.notification_queue: List[ShowNotificationData] = []
        self.awaiting_app_names: Set[str] = set()
        self.known_app_names: Dict[str, str] = dict()
        # Control point writes waiting to be sent, one at a time (see write_control_point).
        self.write_queue: List[Tuple[List[int], str, Any]] = []
        self.write_in_flight = False
        self.detached = False
        # Latest Notification Source event per iOS UID, kept until iOS removes it, so a
        # repeated attributes response still gets its event facts.
        self.events: Dict[int, Notification] = dict()
        # Data Source reassembly: responses can be split across several notifications.
        self.ds_buffer = bytearray()
        self.ds_stall_timer: Optional[int] = None
        # Attribute counts of the notification-attributes requests sent, by UID: needed to
        # know where a (possibly split) response ends.
        self.expected_attributes: Dict[int, int] = dict()

    def detach(self) -> None:
        self.detached = True
        self.write_queue.clear()
        if self.ds_stall_timer is not None:
            GLib.source_remove(self.ds_stall_timer)
            self.ds_stall_timer = None

    # --- Notification Source -------------------------------------------------------
    def on_ns_value(self, value: List[int]) -> None:
        notification = Notification.parse(value)
        if notification.type in (
            EventID.NotificationAdded,
            EventID.NotificationModified,
        ):
            # Upstream turned pre-existing additions into dismissals; fetch them all and
            # pass the flag on instead, so consumers decide what "old" means.
            self.events[notification.id] = notification
            self.ask_for_notification_details(notification)
        else:
            self.events.pop(notification.id, None)
            host_id = self.host_id(notification.id)
            self.device.actionable.pop(host_id, None)
            # Don't emit a Show for it after this Dismiss (it may be waiting for its app's
            # name).
            self.notification_queue = [d for d in self.notification_queue if d.id != host_id]
            self.device.note_dismissed(host_id)
            self.device.server.emit_dismiss_notification(host_id)

    def host_id(self, uid: int) -> int:
        return (self.device.id_base + uid) % UINT_MAX

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
        # AppIdentifier, Title, Subtitle, Message, MessageSize, Date + any action labels.
        count = 6 + notification.has_positive_action() + notification.has_negative_action()
        self.write_control_point(
            msg.to_list(), "notification attributes", ("notification", notification.id, count)
        )

    # --- control point -------------------------------------------------------------
    def write_control_point(self, value: List[int], what: str, request: Any) -> None:
        # Asynchronous with a time limit: a reply that never comes must not freeze the
        # observer. bluetoothd allows one outstanding write per characteristic ("In
        # Progress" otherwise), so writes are queued and sent one at a time -- a
        # reconnect's full list means dozens of them at once.
        self.write_queue.append((value, what, request))
        if not self.write_in_flight:
            self.send_next_write()

    def send_next_write(self) -> None:
        if not self.write_queue or self.detached or not self.device.control_point:
            self.write_in_flight = False
            return
        value, what, request = self.write_queue.pop(0)
        self.write_in_flight = True
        if request[0] == "notification":
            self.expected_attributes[request[1]] = request[2]

        def done(_: Any, error: Optional[str]) -> None:
            if error is not None:
                log.warning(f"Control point write ({what}) failed: {error}")
                self.request_failed(request)
            self.send_next_write()

        bluez.call(
            self.device.control_point, GATT_CHARACTERISTIC, "WriteValue",
            GLib.Variant("(aya{sv})", (bytes(value), {})), done,
        )

    def request_failed(self, request: Any) -> None:
        if request[0] == "notification":
            self.expected_attributes.pop(request[1], None)
        elif request[0] == "app":
            # Don't hold that app's notifications back for the rest of the connection:
            # pass them on with an empty app name (as iOS gave none).
            app_id = request[1]
            self.awaiting_app_names.discard(app_id)
            self.known_app_names.setdefault(app_id, "")
            self.process_queue()

    # --- Data Source ---------------------------------------------------------------
    def on_ds_value(self, value: List[int]) -> None:
        self.ds_buffer += bytes(value)
        while self.ds_buffer:
            parsed = self.parse_data_source(self.ds_buffer)
            if parsed is None:
                break  # incomplete: wait for the rest
            consumed, handler, item = parsed
            del self.ds_buffer[:consumed]
            if handler:
                handler(item)
        self.restart_stall_timer()

    def restart_stall_timer(self) -> None:
        if self.ds_stall_timer is not None:
            GLib.source_remove(self.ds_stall_timer)
            self.ds_stall_timer = None
        if self.ds_buffer:
            self.ds_stall_timer = GLib.timeout_add(DATA_SOURCE_STALL_MS, self.data_source_stalled)

    def data_source_stalled(self) -> bool:
        self.ds_stall_timer = None
        log.warning(f"Incomplete Data Source response dropped ({len(self.ds_buffer)} bytes)")
        self.ds_buffer.clear()
        return False

    def parse_data_source(self, buf: bytearray) -> Optional[Tuple[int, Any, Any]]:
        """(bytes consumed, handler, parsed item) for one complete response, else None."""
        command = buf[0]
        if command == CommandID.GetNotificationAttributes:
            if len(buf) < 5:
                return None
            uid = struct.unpack("<I", bytes(buf[1:5]))[0]
            count = self.expected_attributes.get(uid)
            if count is None:
                log.warning(f"Data Source response for unknown request (uid {uid}); dropped")
                return len(buf), None, None
            pos, attrs = 5, {}
            for _ in range(count):
                if len(buf) < pos + 3:
                    return None
                attr_id = buf[pos]
                length = struct.unpack("<H", bytes(buf[pos + 1 : pos + 3]))[0]
                if len(buf) < pos + 3 + length:
                    return None
                attrs[attr_id] = bytes(buf[pos + 3 : pos + 3 + length]).decode(
                    "utf8", errors="replace"
                )
                pos += 3 + length
            self.expected_attributes.pop(uid, None)
            A = NotificationAttributeID
            item = NotificationAttributes(
                id=uid,
                app_id=attrs.get(A.AppIdentifier, ""),
                title=attrs.get(A.Title, ""),
                subtitle=attrs.get(A.Subtitle, ""),
                message=attrs.get(A.Message, ""),
                date=attrs.get(A.Date, ""),
                message_size=attrs.get(A.MessageSize, ""),
                positive_action=attrs.get(A.PositiveActionLabel),
                negative_action=attrs.get(A.NegativeActionLabel),
            )
            return pos, self.on_notification_attributes, item
        if command == CommandID.GetAppAttributes:
            end = buf.find(b"\0", 1)
            if end < 0:
                return None
            app_id = bytes(buf[1:end]).decode("utf8", errors="replace")
            pos = end + 1
            # One attribute was requested (DisplayName): id, length, value.
            if len(buf) < pos + 3:
                return None
            length = struct.unpack("<H", bytes(buf[pos + 1 : pos + 3]))[0]
            if len(buf) < pos + 3 + length:
                return None
            name = bytes(buf[pos + 3 : pos + 3 + length]).decode("utf8", errors="replace")
            return pos + 3 + length, self.on_app_attributes, AppAttributes(app_id=app_id, app_name=name)
        log.warning(f"Unknown Data Source response (command {command}); dropped")
        return len(buf), None, None

    def on_notification_attributes(self, attrs: NotificationAttributes) -> None:
        event = self.events.get(attrs.id)
        self.device.actionable[self.host_id(attrs.id)] = (
            attrs.positive_action is not None,
            attrs.negative_action is not None,
        )
        self.queue_notification(
            ShowNotificationData(
                device_handle=self.device.path,
                device_name=self.device.name or "",
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
        self.awaiting_app_names.discard(attrs.app_id)
        self.process_queue()

    def queue_notification(self, data: ShowNotificationData) -> None:
        self.notification_queue.append(data)

    def ask_for_app_name(self, app_id: str) -> None:
        self.awaiting_app_names.add(app_id)
        msg = GetAppAttributes(app_id=app_id)
        self.write_control_point(msg.to_list(), "app attributes", ("app", app_id))

    def emit_show(self, data: ShowNotificationData) -> None:
        self.device.note_shown(data)
        self.device.server.emit_show_notification(data)

    def process_queue(self) -> None:
        unprocessed = []
        for data in self.notification_queue:
            if data.app_name != "":
                self.emit_show(data)
            elif data.app_id in self.known_app_names:
                data.app_name = self.known_app_names[data.app_id]
                self.emit_show(data)
            elif data.app_id in self.awaiting_app_names:
                unprocessed.append(data)
            else:
                self.ask_for_app_name(data.app_id)
                unprocessed.append(data)
        self.notification_queue = unprocessed

    def ask_for_action(self, notification_id: int, is_positive: bool) -> None:
        # Only act on ids iOS has listed since the last fresh subscription and not
        # removed since, and only with an action iOS offered. Anything else could be a
        # notification that's gone -- or, after a phone reboot resets UIDs, a different
        # one (e.g. the Accept of the wrong call).
        if notification_id not in self.device.actionable:
            raise InvalidAction(f"Notification {notification_id} isn't on the phone")
        positive, negative = self.device.actionable[notification_id]
        if not (positive if is_positive else negative):
            raise InvalidAction(
                f"Notification {notification_id} has no "
                f"{'positive' if is_positive else 'negative'} action"
            )
        if not self.device.control_point:
            raise InvalidAction("Phone is not connected/subscribed")
        id = (notification_id - self.device.id_base) % UINT_MAX
        msg = PerformNotificationAction(notification_id=id, is_positive=is_positive)
        # Synchronous so the caller hears about iOS rejecting it, but never unbounded.
        # (May fail with "In Progress" while a list is being fetched: retry.)
        bluez.call_sync(
            self.device.control_point, GATT_CHARACTERISTIC, "WriteValue",
            GLib.Variant("(aya{sv})", (bytes(msg.to_list()), {})),
        )
