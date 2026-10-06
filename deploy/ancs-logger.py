"""Debug consumer for ancs4linux on fe-pro.

Writes ancs4linux's D-Bus signals, plus BlueZ connection changes, as JSON lines to
/var/log/ancs4linux/notifications.log. It only listens, so it can be restarted or
replaced (by the MQTT bridge) without touching the phone's BLE connection.
Bodies are logged only when LOG_BODIES=1; otherwise just their length.
"""
import json
import logging
import logging.handlers
import os
from datetime import datetime

from gi.repository import Gio, GLib

LOG_PATH = os.environ.get("LOG_PATH", "/var/log/ancs4linux/notifications.log")
LOG_BODIES = os.environ.get("LOG_BODIES") == "1"

log = logging.getLogger("ancs-logger")
log.setLevel(logging.INFO)
# Bounded, so personal content doesn't pile up: at most ~2 MB on disk.
handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1)
handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(handler)


def write(event, **fields):
    record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event}
    record.update(fields)
    log.info(json.dumps(record, ensure_ascii=False))


def on_show(conn, sender, path, iface, member, params):
    data = json.loads(params.unpack()[0])
    body = data.get("body") or ""
    subtitle = data.get("subtitle") or ""
    fields = {
        "id": data["id"],
        "device": data["device_name"],
        "app_id": data["app_id"],
        "app": data["app_name"],
        "category": data.get("category"),
        "category_count": data.get("category_count"),
        "date": data.get("date"),
        "message_size": data.get("message_size"),
        "ancs_event": data.get("event"),
        "pre_existing": data.get("pre_existing"),
        "important": data.get("important"),
        "silent": data.get("silent"),
        "title": data["title"],
        "subtitle_len": len(subtitle),
        "actions": [data.get("positive_action"), data.get("negative_action")],
        "body_len": len(body),
    }
    if LOG_BODIES:
        fields["subtitle"] = subtitle
        fields["body"] = body
    write("show", **fields)


def on_dismiss(conn, sender, path, iface, member, params):
    write("dismiss", id=params.unpack()[0])


def on_subscribed(conn, sender, path, iface, member, params):
    write("subscribed", device=params.unpack()[0])


def on_subscribe_failed(conn, sender, path, iface, member, params):
    device, error = params.unpack()
    write("subscribe_failed", device=device, error=error)


def on_pairing_code(conn, sender, path, iface, member, params):
    write("pairing_code", pin=params.unpack()[0])


def on_bluez_props(conn, sender, path, iface, member, params):
    interface, changes, _ = params.unpack()
    if interface != "org.bluez.Device1":
        return
    watched = {k: v for k, v in changes.items() if k in ("Connected", "Paired", "Bonded", "ServicesResolved")}
    if watched:
        mac = path.rsplit("/", 1)[-1].removeprefix("dev_").replace("_", ":")
        write("device", mac=mac, **watched)


def main():
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    subscriptions = [
        ("ancs4linux.Observer", "ancs4linux.Observer", "ShowNotification", None, on_show),
        ("ancs4linux.Observer", "ancs4linux.Observer", "DismissNotification", None, on_dismiss),
        ("ancs4linux.Observer", "ancs4linux.Observer", "Subscribed", None, on_subscribed),
        ("ancs4linux.Observer", "ancs4linux.Observer", "SubscribeFailed", None, on_subscribe_failed),
        ("ancs4linux.Advertising", "ancs4linux.Advertising", "PairingCode", None, on_pairing_code),
        ("org.bluez", "org.freedesktop.DBus.Properties", "PropertiesChanged", "org.bluez.Device1", on_bluez_props),
    ]
    for sender, interface, member, arg0, callback in subscriptions:
        bus.signal_subscribe(
            sender, interface, member, None, arg0, Gio.DBusSignalFlags.NONE,
            lambda c, s, p, i, m, params, _u, cb=callback: cb(c, s, p, i, m, params),
            None,
        )
    write("logger_started", bodies=LOG_BODIES)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
