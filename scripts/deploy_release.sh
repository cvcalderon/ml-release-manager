#!/usr/bin/env bash
set -e

RELEASE_NAME="${1:-}"

if [ -z "$RELEASE_NAME" ]; then
  echo "Usage: deploy_release.sh <release_name>"
  exit 1
fi

BASE="/opt/release_manager"
TARGET="${BASE}/releases/${RELEASE_NAME}"

if [ ! -d "$TARGET" ]; then
  echo "ERROR: release not found: $TARGET"
  exit 1
fi

echo "Activating release: $RELEASE_NAME"
sudo ln -sfn "$TARGET" "${BASE}/current"

echo "Restarting systemd service..."
sudo systemctl restart release-service

echo "Done."
