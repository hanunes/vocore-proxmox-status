#!/bin/sh
# Run as root from this folder on each node. Safe to run again.
set -eu
cd "$(dirname "$0")"

apt-get update -qq
apt-get install -y --no-install-recommends python3-pil python3-usb fonts-dejavu-core
id vocore-status >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /usr/sbin/nologin vocore-status

install -d /opt/vocore-status
install -m 755 vocore_status.py /opt/vocore-status/
install -m 644 vocore-status.service /etc/systemd/system/
install -d -m 750 -g vocore-status /etc/vocore-status
[ -e /etc/vocore-status/config.toml ] || install -m 644 config.toml /etc/vocore-status/

cat > /etc/udev/rules.d/98-vocore-status.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="c872", ATTR{idProduct}=="1004", MODE="0660", GROUP="vocore-status"
EOF
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb --attr-match=idVendor=c872

systemctl daemon-reload
systemctl enable vocore-status.service
systemctl restart vocore-status.service
echo "Installed. Token file: /etc/vocore-status/token"
