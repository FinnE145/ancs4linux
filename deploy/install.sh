#!/bin/bash
# Installs ancs4linux with the patches in patches/. Run with sudo. Safe to re-run.
# See README.md; settings in ancs4linux.env (copied to /etc/ancs4linux.env on first install).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Settings: the installed env file wins over the default one next to this script.
ENV_FILE=/etc/ancs4linux.env; [ -e "$ENV_FILE" ] || ENV_FILE=ancs4linux.env
ANCS_USER=$(. "$ENV_FILE"; echo "${ANCS_USER:-}")
ANCS_USER=${ANCS_USER:-${SUDO_USER:-}}
[ -n "$ANCS_USER" ] && id "$ANCS_USER" >/dev/null 2>&1 || { echo "Set ANCS_USER in $ENV_FILE (or run via sudo from that user)."; exit 1; }

# Mirror output to a log next to this script, readable by ANCS_USER.
touch install.log && chown "$ANCS_USER:" install.log
exec > >(tee install.log) 2>&1

COMMIT=b658546f08d1468f6d79aa900cc7faa9d938837d   # upstream HEAD, 2026-08-29
PREFIX=/opt/ancs4linux

apt-get install -y python3-venv

# The advertiser needs bluetoothd >= 5.87 (see ancs4linux-advertise.service).
bluez=$(dpkg-query -W -f='${Version}' bluez)
dpkg --compare-versions "$bluez" ge 5.87 || { echo "bluez $bluez is too old; install 5.87 first (README.md)"; exit 1; }

groupadd -f ancs4linux
usermod -aG ancs4linux "$ANCS_USER"

install -d "$PREFIX" "$PREFIX/bin" "$PREFIX/deploy"
[ -d "$PREFIX/src/.git" ] || git clone https://github.com/pzmarzly/ancs4linux "$PREFIX/src"
git -C "$PREFIX/src" fetch --quiet origin
git -C "$PREFIX/src" checkout --quiet --force "$COMMIT"
# Files added by an earlier run's patches are untracked, and checkout leaves them behind
# (then `git apply` refuses to create them again).
git -C "$PREFIX/src" clean -fdq
# Local fixes on top of upstream (BLE/ANCS-level only; see README.md).
for p in patches/*.patch; do git -C "$PREFIX/src" apply "$PWD/$p"; done

# System site packages give us apt's python3-gi, so PyGObject needn't be compiled.
# Hence --no-deps for ancs4linux itself (it pins PyGObject>=3.50; apt has 3.48, which suffices).
[ -x "$PREFIX/venv/bin/python3" ] || python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet 'dasbus==1.7' 'typer==0.25.1'
"$PREFIX/venv/bin/pip" install --quiet --no-deps --force-reinstall "$PREFIX/src"

install -m 755 ancs-pair "$PREFIX/bin/ancs-pair"
install -m 644 ancs-logger.py "$PREFIX/bin/ancs-logger.py"
install -m 644 ancs-advertiser.py "$PREFIX/bin/ancs-advertiser.py"
ln -sf "$PREFIX/bin/ancs-pair" /usr/local/bin/ancs-pair
ln -sf "$PREFIX/venv/bin/ancs4linux-ctl" /usr/local/bin/ancs4linux-ctl

install -m 644 "$PREFIX/src/autorun/ancs4linux-observer.xml" /etc/dbus-1/system.d/ancs4linux-observer.conf
install -m 644 "$PREFIX/src/autorun/ancs4linux-advertising.xml" /etc/dbus-1/system.d/ancs4linux-advertising.conf
for unit in observer advertising advertise logger; do
    sed "s/@ANCS_USER@/$ANCS_USER/g" "ancs4linux-$unit.service" > "/etc/systemd/system/ancs4linux-$unit.service"
    chmod 644 "/etc/systemd/system/ancs4linux-$unit.service"
done
[ -e /etc/ancs4linux.env ] || install -m 644 ancs4linux.env /etc/ancs4linux.env

# Migration from the BlueZ 5.72 setup: the btmgmt advertisement and the 30 s check timer are
# replaced by ancs-advertiser.py. (Leftover HCI_INDEX/ADV_* lines in the env file are unused.)
if [ -e /etc/systemd/system/ancs4linux-advert-check.timer ]; then
    systemctl disable --now ancs4linux-advert-check.timer || true
    rm -f /etc/systemd/system/ancs4linux-advert-check.{timer,service} "$PREFIX/bin/ancs-advert-check"
    timeout 10 script -qec "btmgmt --index 0 rm-adv 9" /dev/null || true
fi

# Keep a copy of these deploy files next to the install, for reference/re-runs.
if [ "$PWD" != "$PREFIX/deploy" ]; then
    rm -f "$PREFIX"/deploy/ancs-advert-check "$PREFIX"/deploy/*.timer "$PREFIX"/deploy/ancs4linux-advert-check.service
    install -m 644 -t "$PREFIX/deploy" ancs4linux.env ancs-logger.py ancs-advertiser.py ancs-pair ./*.service install.sh
    install -d "$PREFIX/deploy/patches"
    install -m 644 -t "$PREFIX/deploy/patches" patches/*.patch
fi

systemctl reload dbus
systemctl daemon-reload
systemctl enable ancs4linux-observer ancs4linux-advertising ancs4linux-advertise ancs4linux-logger
systemctl restart ancs4linux-observer ancs4linux-advertising ancs4linux-logger
systemctl restart ancs4linux-advertise

systemctl --no-pager --lines=0 status ancs4linux-observer ancs4linux-advertising ancs4linux-advertise ancs4linux-logger
