"""ancs-reconnect.py against a fake machine: when it acts, how often, and which stage it reports.

Run with any pytest (no BlueZ or root needed): python3 -m pytest deploy/tests
"""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ancs_reconnect", Path(__file__).resolve().parent.parent / "ancs-reconnect.py"
)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

H = 3600


def status(connected=False, subscribed=False, paired=True):
    return {"devices": [{"paired": paired, "connected": connected, "subscribed": subscribed}]}


class FakeSystem:
    """Time only moves through sleep(); the phone connects when `connect_when` says so."""

    def __init__(self, now=1_000_000.0, connect_when=None):
        self.t = now
        self.calls = []
        self.status = status()
        self.observer_running = True
        self.restarted = False
        # None: never; "stopped": once the observer is stopped; "restart": after bluetoothd's.
        self.connect_when = connect_when
        self.event_count = 100

    def observer_status(self):
        return self.status if self.observer_running else None

    def adapter(self, prop):
        return {"Discovering": self.observer_running, "ActiveInstances": 1}[prop]

    def paired_connected(self):
        if self.connect_when == "stopped" and not self.observer_running:
            return ["/org/bluez/hci0/dev_X"]
        if self.connect_when == "restart" and self.restarted:
            return ["/org/bluez/hci0/dev_X"]
        return []

    def events(self):
        self.event_count += 7
        return self.event_count

    def links(self):
        return "none"

    def systemctl(self, *args):
        self.calls.append(args)
        if args == ("stop", "ancs4linux-observer.service"):
            self.observer_running = False
        elif args == ("start", "ancs4linux-observer.service"):
            self.observer_running = True
        elif args == ("restart", "bluetooth.service"):
            self.restarted = True

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "state.json")


def tick(system, state_file, advance=600):
    system.t += advance
    lines = []
    action = rc.run(system, state_file, lines.append)
    return action, lines


def test_decide_counts_from_first_sight_then_every_6_hours_6_times():
    # Wrong spacing or a wrong cap would show as a different list of attempt times.
    state, t, times = {}, 0.0, []
    for _ in range(int(60 * H / 600)):
        state, action = rc.decide(state, False, t)
        if action in ("attempt", "gave_up"):
            times.append(t)
        t += 600
    assert [x / H for x in times] == [6, 12, 18, 24, 30, 36]
    assert state["attempts"] == 6


def test_decide_last_attempt_is_gave_up_and_live_resets():
    state = {"down_since": 0.0, "attempts": 5, "last_attempt": 30 * H}
    state, action = rc.decide(state, False, 36 * H)
    assert action == "gave_up"
    assert rc.decide(state, False, 100 * H) == (state, "none")
    assert rc.decide(state, True, 100 * H) == ({}, "recovered")
    assert rc.decide({}, True, 0.0) == ({}, "none")


def test_live_needs_connected_and_subscribed():
    assert not rc.live_from_status(None)
    assert not rc.live_from_status(status(connected=True, subscribed=False))
    assert not rc.live_from_status(status(connected=True, subscribed=True, paired=False))
    assert rc.live_from_status(status(connected=True, subscribed=True))


def test_hci_events_parses_hciconfig():
    out = "hci0:\tType: Primary  Bus: USB\n\tRX bytes:617384946 acl:941450 sco:0 events:5047181 errors:0\n"
    assert rc.hci_events(out) == 5047181
    assert rc.hci_events("garbage") is None


def test_no_action_before_6_hours_and_state_persists(state_file):
    system = FakeSystem()
    action, lines = tick(system, state_file)
    assert action == "none" and lines == ["phone not connected and subscribed; counting from now"]
    for _ in range(int(6 * H / 600) - 1):
        action, _ = tick(system, state_file)
        assert action == "none"
    assert system.calls == []
    assert json.load(open(state_file))["attempts"] == 0


def test_stage_1_brings_it_back_without_restarting_bluetoothd(state_file):
    system = FakeSystem(connect_when="stopped")
    tick(system, state_file)
    action, lines = tick(system, state_file, advance=6 * H)
    assert action == "attempt"
    assert system.calls == [
        ("stop", "ancs4linux-observer.service"),
        ("start", "ancs4linux-observer.service"),
    ]
    assert "stage 1, no bluetoothd restart" in lines[-1]
    # Back and subscribed at the next tick: state cleared, and the attempt count reported.
    system.status = status(connected=True, subscribed=True)
    action, lines = tick(system, state_file)
    assert action == "recovered" and "after 1 attempt(s)" in lines[0]
    assert json.load(open(state_file)) == {}


def test_stage_2_restarts_bluetoothd_and_brings_everything_back(state_file):
    system = FakeSystem(connect_when="restart")
    tick(system, state_file)
    _, lines = tick(system, state_file, advance=6 * H)
    assert ("restart", "bluetooth.service") in system.calls
    # The observer is started again after stage 1 even though stage 2 follows.
    assert system.calls[:2] == [
        ("stop", "ancs4linux-observer.service"),
        ("start", "ancs4linux-observer.service"),
    ]
    for unit in ("ancs4linux-observer", "ancs4linux-advertising", "ancs4linux-advertise"):
        assert ("start", f"{unit}.service") in system.calls[2:]
    assert "(stage 2)" in lines[-1]


def test_away_phone_six_attempts_then_silence(state_file):
    system = FakeSystem(connect_when=None)
    tick(system, state_file)
    results = []
    for _ in range(int(48 * H / 600)):
        _, lines = tick(system, state_file)
        results += [line for line in lines if line.startswith("RESULT")]
    assert len(results) == 6
    assert all("did not connect" in r for r in results)
    assert system.calls.count(("restart", "bluetooth.service")) == 6


def test_observer_left_running_even_if_wait_raises(state_file):
    system = FakeSystem()

    def boom():
        raise RuntimeError("bus gone")

    system.paired_connected = boom
    tick(system, state_file)
    with pytest.raises(RuntimeError):
        tick(system, state_file, advance=6 * H)
    assert system.calls[-1] == ("start", "ancs4linux-observer.service")


def test_no_paired_phone_does_nothing(state_file):
    system = FakeSystem()
    system.status = {"devices": []}
    for _ in range(int(10 * H / 600)):
        assert tick(system, state_file)[0] == "none"
    assert system.calls == [] and json.load(open(state_file)) == {}
