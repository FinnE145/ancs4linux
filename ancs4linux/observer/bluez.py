"""Talking to bluetoothd without per-object proxies (added on fe-pro).

Upstream created a dasbus proxy, and with it a D-Bus match rule, for every BlueZ object it
saw and never released them. With LE discovery running, bluetoothd lists dozens of passing
devices a minute, so the observer hit dbus-daemon's 512-match-rule limit within about half
an hour; after that new subscriptions (the phone's ANCS characteristics) silently received
nothing. Proxies also introspect synchronously, without a timeout, on first use.

Instead:
- a fixed handful of bus-wide signal subscriptions (InterfacesAdded/Removed, and
  PropertiesChanged filtered by interface name), dispatched by object path;
- method calls made directly on the connection, always asynchronous with a time limit
  (synchronous ones only where a D-Bus caller is waiting for the answer, also limited).

Values are passed on unpacked into plain Python types.
"""
import logging
from typing import Any, Callable, Dict, List, Optional

from gi.repository import Gio, GLib  # type: ignore # dynamic

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
# Every call to bluetoothd gets a limit; the D-Bus libraries' default is to wait forever.
CALL_TIMEOUT_MS = 10_000

Done = Callable[[Any, Optional[str]], None]  # (result, error message or None)


def connection() -> Gio.DBusConnection:
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(
    path: str,
    interface: str,
    method: str,
    params: Optional[GLib.Variant] = None,
    done: Optional[Done] = None,
    timeout_ms: int = CALL_TIMEOUT_MS,
) -> None:
    """Asynchronous call to bluetoothd; done(result, None) or done(None, error)."""

    def finish(conn: Gio.DBusConnection, result: Gio.AsyncResult, _: Any) -> None:
        try:
            reply = conn.call_finish(result)
        except GLib.Error as e:
            if done:
                done(None, e.message)
            else:
                log.warning(f"{interface}.{method} on {path} failed: {e.message}")
            return
        if done:
            unpacked = reply.unpack() if reply is not None else ()
            done(unpacked[0] if len(unpacked) == 1 else unpacked, None)

    connection().call(
        BLUEZ, path, interface, method, params, None,
        Gio.DBusCallFlags.NONE, timeout_ms, None, finish, None,
    )


def call_sync(
    path: str,
    interface: str,
    method: str,
    params: Optional[GLib.Variant] = None,
    timeout_ms: int = CALL_TIMEOUT_MS,
) -> Any:
    """Blocking call with a time limit; raises GLib.Error. Only where a caller waits."""
    reply = connection().call_sync(
        BLUEZ, path, interface, method, params, None,
        Gio.DBusCallFlags.NONE, timeout_ms, None,
    )
    unpacked = reply.unpack() if reply is not None else ()
    return unpacked[0] if len(unpacked) == 1 else unpacked


def get_all(path: str, interface: str, done: Done) -> None:
    call(path, "org.freedesktop.DBus.Properties", "GetAll", GLib.Variant("(s)", (interface,)), done)


def on_properties_changed(
    interface: str, handler: Callable[[str, Dict[str, Any], List[str]], None]
) -> int:
    """One bus-wide subscription for PropertiesChanged of `interface` on any BlueZ object."""

    def dispatch(conn, sender, path, iface, member, params, _user):  # noqa: ANN001
        changed_interface, changes, invalidated = params.unpack()
        if changed_interface != interface:
            return
        try:
            handler(path, changes, invalidated)
        except Exception:
            log.exception(f"handling PropertiesChanged({interface}) on {path} failed")

    return connection().signal_subscribe(
        BLUEZ, "org.freedesktop.DBus.Properties", "PropertiesChanged", None, interface,
        Gio.DBusSignalFlags.NONE, dispatch, None,
    )


def on_object_manager(
    added: Callable[[str, Dict[str, Dict[str, Any]]], None],
    removed: Callable[[str, List[str]], None],
) -> None:
    def dispatch(conn, sender, path, iface, member, params, _user):  # noqa: ANN001
        try:
            if member == "InterfacesAdded":
                added(*params.unpack())
            else:
                removed(*params.unpack())
        except Exception:
            log.exception(f"handling {member} failed")

    for member in ("InterfacesAdded", "InterfacesRemoved"):
        connection().signal_subscribe(
            BLUEZ, "org.freedesktop.DBus.ObjectManager", member, "/", None,
            Gio.DBusSignalFlags.NONE, dispatch, None,
        )


def managed_objects() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """At startup only; blocking but time-limited."""
    return call_sync("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
