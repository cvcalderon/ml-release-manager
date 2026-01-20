# Deployment Notes (Current CT implementation)

This repository contains a working MVP for an internal release manager system.

## What the system does
- Upload a Release Bundle (.zip)
- Validate bundle structure + python import checks
- Install in: `/opt/release_manager/releases/<release_name>/`
- Deploy from GUI by switching: `/opt/release_manager/current` (symlink)
- Restart deployed API service using systemd
- Stop/Deactivate from GUI (stop service + remove `current`)
- Delete a release from GUI only if it is not ACTIVE
- Download canonical `release_bundle.zip` from GUI

## Current services
- Admin Panel (FastAPI):
  - systemd: `ml-release-manager-admin`
  - listens: `127.0.0.1:9000`
- Deployed Service (FastAPI from release):
  - systemd: `ml-release-service`
  - listens: `0.0.0.0:8000`

## Nginx routes
- `/admin/` -> `127.0.0.1:9000`
- `/api/`   -> `127.0.0.1:8000`

Important: `proxy_pass` is configured without a trailing slash to preserve paths.

## Critical rule: `current` must be a symlink
The deployed service runs from:

`/opt/release_manager/current`

This path MUST be a symlink directly pointing to the active release folder:

✅ `/opt/release_manager/current -> /opt/release_manager/releases/<release>`

Wrong layout (breaks imports):

❌ `/opt/release_manager/current/<release> -> ...`
