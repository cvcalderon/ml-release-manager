#!/usr/bin/env bash
set -e

echo "[1/4] Creating base directories..."
sudo mkdir -p /opt/release_manager/{releases,runtime/uploads,runtime/logs}

echo "[2/4] Creating service user (if missing)..."
if ! id -u serviceuser >/dev/null 2>&1; then
  sudo adduser --disabled-password --gecos "" serviceuser
fi

echo "[3/4] Ensuring current is not a directory..."
if [ -d /opt/release_manager/current ] && [ ! -L /opt/release_manager/current ]; then
  sudo rm -rf /opt/release_manager/current
fi

echo "[4/4] Setting ownership..."
sudo chown -R serviceuser:serviceuser /opt/release_manager

echo "[DONE] Base installation completed."
