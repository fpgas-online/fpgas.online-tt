#!/bin/sh
set -e
systemctl daemon-reload || true
udevadm control --reload || true
