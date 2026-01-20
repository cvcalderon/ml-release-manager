# Config Snapshots (CT)

This file documents the configuration files used in the current working CT.

## systemd
### Admin Panel
Path:
- `/etc/systemd/system/ml-release-manager-admin.service`

Purpose:
- Runs FastAPI admin panel as `serviceuser`

### Deployed Service
Path:
- `/etc/systemd/system/ml-release-service.service`

Purpose:
- Runs release API from `/opt/release_manager/current`

## Nginx
Path:
- `/etc/nginx/sites-available/ml-release-manager`
Enabled:
- `/etc/nginx/sites-enabled/ml-release-manager`

Routes:
- `/admin/` -> `127.0.0.1:9000`
- `/api/`   -> `127.0.0.1:8000`

## sudoers
Path:
- `/etc/sudoers.d/ml-release-manager`

Purpose:
- Allow `serviceuser` to run systemctl commands without password for:
  - start/stop/restart/status `ml-release-service`
