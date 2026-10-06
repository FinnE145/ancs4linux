import logging
import os
import socket

import typer
from gi.repository import GLib  # type: ignore # dynamic

from ancs4linux.common.dbus import EventLoop, SystemBus
from ancs4linux.observer.scanner import Scanner
from ancs4linux.observer.server import ObserverServer

log = logging.getLogger(__name__)
app = typer.Typer()


@app.command()
def main(
    observer_dbus: str = typer.Option("ancs4linux.Observer", help="Service path")
) -> None:
    logging.basicConfig(level=logging.DEBUG)
    loop = EventLoop()

    server = ObserverServer()
    scanner = Scanner(server)
    server.set_scanner(scanner)
    server.register()
    SystemBus().register_service(observer_dbus)

    log.info("Observing devices...")
    scanner.start_observing()
    start_watchdog()
    loop.run()


def start_watchdog() -> None:
    """Ping systemd's watchdog from the main loop (added on fe-pro).

    The pings only go out while the loop is running, so if anything blocks it again,
    systemd (WatchdogSec= in the unit) restarts the observer instead of it hanging silently.
    """
    usec = os.environ.get("WATCHDOG_USEC")
    path = os.environ.get("NOTIFY_SOCKET")
    if not usec or not path:
        return
    address = "\0" + path[1:] if path.startswith("@") else path
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def ping() -> bool:
        sock.sendto(b"WATCHDOG=1", address)
        return True

    ping()
    GLib.timeout_add(max(1000, int(usec) // 3000), ping)
    log.info(f"systemd watchdog: pinging every {max(1, int(usec) // 3_000_000)} s")
