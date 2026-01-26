# ML Release Manager — Replication Manual (CT / Intranet)

This manual describes how to replicate the **ml-release-manager** MVP on a fresh Linux node (CT) so that:

- The **Admin Panel** is available at: `http://<server>/admin/`
- The deployed service API is available at: `http://<server>/api/`
- Releases can be installed, validated, have dependencies installed, activated/deactivated, downloaded and deleted via GUI.

> ✅ Target audience: internal operators (intranet only)

---

## 0) What you will end up with

### System layout (runtime)
```
/opt/release_manager/
  releases/
    <release_name>/
      service/
      assets/
      release.json
      validation_report.json
      release_bundle.zip
      .venv/                  (per-release venv)
  current -> /opt/release_manager/releases/<active_release>
  runtime/
    uploads/
    logs/
  venv/                       (shared venv for Admin Panel only)
```

### Services (systemd)
- **Admin panel:** `ml-release-manager-admin`  
  Listens on `127.0.0.1:9000`

- **Deployed service:** `ml-release-service`  
  Listens on `0.0.0.0:8000` (recommended to expose through Nginx as `/api/`)

### Reverse proxy (Nginx)
- `/admin/` → `127.0.0.1:9000`
- `/api/` → `127.0.0.1:8000`

---

## 1) Requirements on the server (CT)

### OS assumptions
- Ubuntu / Debian-based Linux
- systemd enabled
- internet access to install packages + python packages

### Install OS packages
Run as **root**:

```bash
sudo apt update
sudo apt install -y \
  git curl unzip \
  python3 python3-venv python3-pip \
  nginx
```

Optional but recommended tools:
```bash
sudo apt install -y jq tree
```

---

## 2) Clone the repository

Run as **serviceuser** (recommended) or root:

```bash
cd /home/serviceuser
git clone https://github.com/cvcalderon/ml-release-manager.git
cd ml-release-manager
```

> If `serviceuser` does not exist yet, run the base install script first (next step).

---

## 3) Base installation (directories + user)

### Script: `scripts/install_base.sh`
This script creates:
- `/opt/release_manager/...`
- user `serviceuser`
- correct ownership

Run as **root**:

```bash
cd /home/serviceuser/ml-release-manager
bash scripts/install_base.sh
```

Expected output includes:
- `/opt/release_manager/releases`
- `/opt/release_manager/runtime/uploads`
- `/opt/release_manager/runtime/logs`
- ownership set to `serviceuser:serviceuser`

---

## 4) Shared Python environment for Admin Panel

The Admin Panel runs with a shared venv:

```
/opt/release_manager/venv/
```

### Script: `scripts/install_admin_panel.sh`
This script should:
- Create `/opt/release_manager/venv`
- Install admin panel dependencies
- Optionally install `uvicorn`, `fastapi`, `jinja2`, etc.

Run as **root**:

```bash
cd /home/serviceuser/ml-release-manager
bash scripts/install_admin_panel.sh
```

### Admin Panel requirements
Make sure `admin_panel/requirements.txt` exists and contains **at least**:

- `fastapi`
- `uvicorn`
- `jinja2`
- `python-multipart`
- `packaging`

Example install (manual fallback):
```bash
sudo -u serviceuser /opt/release_manager/venv/bin/python -m pip install --upgrade pip
sudo -u serviceuser /opt/release_manager/venv/bin/python -m pip install -r admin_panel/requirements.txt
```

---

## 5) systemd services

This project includes **live CT snapshots** under `infra/systemd/`.
Use them as reference and copy them into `/etc/systemd/system/`.

### 5.1 Admin Panel service

Create:

`/etc/systemd/system/ml-release-manager-admin.service`

Content (based on live snapshot):

```ini
[Unit]
Description=ML Release Manager - Admin Panel
After=network.target

[Service]
User=serviceuser
WorkingDirectory=/home/serviceuser/ml-release-manager
ExecStart=/opt/release_manager/venv/bin/uvicorn admin_panel.app.main:app --host 127.0.0.1 --port 9000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ml-release-manager-admin
sudo systemctl status ml-release-manager-admin --no-pager
```

Test local:
```bash
curl -sS http://127.0.0.1:9000/admin/install | head
```

---

### 5.2 Deployed Service (runtime service)

Create:

`/etc/systemd/system/ml-release-service.service`

Content (recommended deployment model: **run from `/opt/release_manager/current`**):

```ini
[Unit]
Description=ML Release Manager - Deployed Service (per-release venv)
After=network.target

[Service]
User=serviceuser
WorkingDirectory=/opt/release_manager/current

ExecStartPre=/usr/bin/test -x /opt/release_manager/current/.venv/bin/python
ExecStart=/opt/release_manager/current/.venv/bin/python -m uvicorn service.app:app --host 0.0.0.0 --port 8000

Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Enable (it may fail until a release is activated):

```bash
sudo systemctl daemon-reload
sudo systemctl enable ml-release-service
```

> ✅ **Important rule**
>
> `/opt/release_manager/current` must be a **symlink directly pointing to a release folder**:
>
> ✅ `/opt/release_manager/current -> /opt/release_manager/releases/<release>`
>
> ❌ Wrong (breaks imports):
> `/opt/release_manager/current/<release> -> ...`

---

## 6) sudoers (allow GUI to manage systemd)

The Admin Panel runs as `serviceuser` and must control only these commands **without password**:

- `systemctl start/stop/restart/status ml-release-service`
- `journalctl -u ml-release-service -n 50`

Create:

`/etc/sudoers.d/ml-release-manager`

Example:
```sudoers
serviceuser ALL=(root) NOPASSWD: /usr/bin/systemctl start ml-release-service
serviceuser ALL=(root) NOPASSWD: /usr/bin/systemctl stop ml-release-service
serviceuser ALL=(root) NOPASSWD: /usr/bin/systemctl restart ml-release-service
serviceuser ALL=(root) NOPASSWD: /usr/bin/systemctl status ml-release-service --no-pager
serviceuser ALL=(root) NOPASSWD: /usr/bin/journalctl -u ml-release-service -n 50 --no-pager
```

Permissions + validation:

```bash
sudo chmod 0440 /etc/sudoers.d/ml-release-manager
sudo visudo -cf /etc/sudoers.d/ml-release-manager
```

---

## 7) Nginx (reverse proxy)

Create a site file:

`/etc/nginx/sites-available/ml-release-manager`

Minimal recommended config:
```nginx
server {
    listen 80;
    server_name _;

    # Admin Panel
    location /admin/ {
        proxy_pass http://127.0.0.1:9000/admin/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Deployed API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Enable site:
```bash
sudo ln -sfn /etc/nginx/sites-available/ml-release-manager /etc/nginx/sites-enabled/ml-release-manager
sudo nginx -t
sudo systemctl restart nginx
```

Check:
- `http://<server>/admin/install`

---

## 8) First run smoke test

### 8.1 Check admin panel service
```bash
sudo systemctl status ml-release-manager-admin --no-pager
```

### 8.2 Open GUI
- `http://<server>/admin/install`

---

## 9) Release bundle installation workflow (GUI)

### Install (Upload)
In **Install Release**, upload a `.zip` and provide:
- `release_name`
- optional `description`
- (optional) `api_port`

The system will:
- extract
- create/complete `release.json`
- validate (structure + venv + missing deps + compile/import)
- generate `release_bundle.zip` canonical artifact

Release folder is created at:
```
/opt/release_manager/releases/<release_name>/
```

---

## 10) Dependencies per release (GUI)

A release can declare dependencies inside `release.json`:

```json
{
  "dependencies": {
    "pip": [
      "numpy",
      "pandas>=2.0",
      "uvicorn[standard]>=0.25"
    ]
  }
}
```

### Validation behavior
- If dependencies are missing in `<release>/.venv`, the release becomes **NOT VALIDATED**
- You can click **Install Dependencies** (progress is tracked)
- Then re-run **Update & Validate**

> The system also includes **base runtime dependencies by service type**, e.g. for FastAPI:
- fastapi
- uvicorn

---

## 11) Activate / Deactivate / Delete

### Activate
A release can be activated only if it is **VALID**.
Activation does:
1. `current -> releases/<release>`
2. `systemctl restart ml-release-service`
3. healthcheck retry loop

### Deactivate
Stops runtime API and removes `/opt/release_manager/current`

### Delete
Only possible if the release is **NOT ACTIVE**.

---

## 12) Download release bundle

Every release card has **Download**:
- Returns the canonical `release_bundle.zip`
- If missing, it is generated automatically

---

## 13) Preparing a ZIP release bundle (Windows 11)

On Windows:
1. Create a folder like:
   ```
   example_v1/
     service/
       app.py
       __init__.py
     assets/
       ...
     release.json (optional)
   ```

2. Zip the **contents** of the folder, not the folder itself.

Correct ZIP root layout must contain:
- `service/`
- `assets/`
- `release.json` (optional)

✅ Good:
```
release.zip
  service/app.py
  assets/...
```

❌ Bad:
```
release.zip
  example_v1/service/app.py
  example_v1/assets/...
```

---

## 14) Troubleshooting

### 502 Bad Gateway (Nginx)
Check:
```bash
sudo systemctl status nginx --no-pager
sudo systemctl status ml-release-manager-admin --no-pager
sudo journalctl -u ml-release-manager-admin -n 80 --no-pager
```

### Admin Panel keeps restarting
Usually missing python modules (e.g. `packaging`).
Fix:
```bash
sudo -u serviceuser /opt/release_manager/venv/bin/python -m pip install -r /home/serviceuser/ml-release-manager/admin_panel/requirements.txt
sudo systemctl restart ml-release-manager-admin
```

### Deployed service fails to start
Check logs:
```bash
sudo journalctl -u ml-release-service -n 120 --no-pager
```

Common causes:
- `/opt/release_manager/current` missing or wrong symlink
- missing `uvicorn` in the runtime venv (if using per-release venv)
- missing dependencies required by the release

---

## 15) Reference docs included in the repo
- `docs/install_manual.md`
- `docs/RELEASE_BUNDLE_SPEC_v1.txt`

---

## 16) Quick checklist (replication success)

✅ Admin Panel works  
- `http://<server>/admin/install`

✅ Nginx routes work  
- `/admin/` → Admin Panel
- `/api/` → deployed API

✅ You can:
- install a release ZIP
- validate it
- install missing dependencies
- activate it (deploy)
- stop/deactivate
- delete it (when not active)
- download `release_bundle.zip`

---

**End of manual**
