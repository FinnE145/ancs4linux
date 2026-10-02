import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from ancs4linux.observer.ancs.constants import (
    CommandID,
    EventFlag,
    EventID,
    NotificationAttributeID,
)


def parse_string(data: bytearray) -> Tuple[str, bytearray]:
    (type, size), data = struct.unpack("<BH", data[:3]), data[3:]
    bytes, data = data[:size], data[size:]
    return bytes.decode("utf8", errors="replace"), data


@dataclass
class Notification:
    id: int
    type: EventID
    flags: EventFlag
    category: int = 0
    category_count: int = 0

    @classmethod
    def parse(cls, data: bytes) -> "Notification":
        [type, flags, category, count, id] = struct.unpack("<BBBBI", bytearray(data))
        return cls(
            id=id, type=type, flags=flags, category=category, category_count=count
        )

    def is_preexisting(self) -> bool:
        return self.flags & EventFlag.PreExisting > 0

    def is_fresh(self) -> bool:
        return not self.is_preexisting()

    def is_silent(self) -> bool:
        return self.flags & EventFlag.Silent > 0

    def is_important(self) -> bool:
        return self.flags & EventFlag.Important > 0

    def has_positive_action(self) -> bool:
        return self.flags & EventFlag.PositiveAction > 0

    def has_negative_action(self) -> bool:
        return self.flags & EventFlag.NegativeAction > 0


@dataclass
class DataSourceEvent:
    type: CommandID
    body: bytearray

    @classmethod
    def parse(cls, data: bytes) -> "DataSourceEvent":
        msg = bytearray(data)
        type, msg = struct.unpack("<B", msg[:1])[0], msg[1:]
        return cls(type=type, body=msg)

    def as_notification_attributes(self) -> "NotificationAttributes":
        assert self.type == CommandID.GetNotificationAttributes
        return NotificationAttributes.parse(self.body)

    def as_app_attributes(self) -> "AppAttributes":
        assert self.type == CommandID.GetAppAttributes
        return AppAttributes.parse(self.body)


@dataclass
class NotificationAttributes:
    id: int
    app_id: str
    title: str
    message: str
    positive_action: Optional[str]
    negative_action: Optional[str]
    subtitle: str = ""
    date: str = ""
    message_size: str = ""

    @classmethod
    def parse(cls, data: bytes) -> "NotificationAttributes":
        msg = bytearray(data)
        id, msg = struct.unpack("<I", msg[:4])[0], msg[4:]
        # Read attributes by their ID rather than assuming the order they come in.
        attrs = {}
        while len(msg) >= 3:
            attr_id = msg[0]
            attrs[attr_id], msg = parse_string(msg)
        return cls(
            id=id,
            app_id=attrs.get(NotificationAttributeID.AppIdentifier, ""),
            title=attrs.get(NotificationAttributeID.Title, ""),
            subtitle=attrs.get(NotificationAttributeID.Subtitle, ""),
            message=attrs.get(NotificationAttributeID.Message, ""),
            date=attrs.get(NotificationAttributeID.Date, ""),
            message_size=attrs.get(NotificationAttributeID.MessageSize, ""),
            positive_action=attrs.get(NotificationAttributeID.PositiveActionLabel),
            negative_action=attrs.get(NotificationAttributeID.NegativeActionLabel),
        )


@dataclass
class AppAttributes:
    app_id: str
    app_name: str

    @classmethod
    def parse(cls, data: bytes) -> "AppAttributes":
        msg = bytearray(data)
        app_id_bytes, msg = msg.split(b"\0", 1)
        app_id = app_id_bytes.decode("utf8", errors="replace")
        if len(msg) == 0:
            app_name = ""
        else:
            app_name_size, msg = struct.unpack("<BH", msg[:3])[1], msg[3:]
            app_name_bytes, msg = msg[:app_name_size], msg[app_name_size:]
            app_name = app_name_bytes.decode("utf8", errors="replace")
        return cls(app_id=app_id, app_name=app_name)
