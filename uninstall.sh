#!/bin/sh
# Run as root on each node. The API token and user are removed separately.
set -u
systemctl disable --now vocore-status.service
rm -f /etc/systemd/system/vocore-status.service /etc/udev/rules.d/98-vocore-status.rules
rm -rf /opt/vocore-status /etc/vocore-status
systemctl daemon-reload
udevadm control --reload-rules
userdel vocore-status
echo "Removed. python3-pil, python3-usb and fonts-dejavu-core were left installed."
