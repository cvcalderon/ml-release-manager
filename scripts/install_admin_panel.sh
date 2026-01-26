#!/usr/bin/env bash
set -e

echo "[1/4] Creating shared venv for Admin Panel..."
sudo mkdir -p /opt/release_manager
sudo chown -R serviceuser:serviceuser /opt/release_manager

sudo -u serviceuser python3 -m venv /opt/release_manager/venv

echo "[2/4] Upgrading pip tooling..."
sudo -u serviceuser /opt/release_manager/venv/bin/python -m pip install --upgrade pip setuptools wheel

echo "[3/4] Installing Admin Panel requirements..."
sudo -u serviceuser /opt/release_manager/venv/bin/python -m pip install -r /home/serviceuser/ml-release-manager/admin_panel/requirements.txt

echo "[4/4] Done."
