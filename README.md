# Release Manager (Intranet)

A generic internal release and deployment manager for versioned service bundles.

## What it does
- Upload a single `.zip` Release Bundle
- Validate structure + entrypoint import + optional plugin validation
- Install as a versioned folder under `releases/<release_name>/`
- Generate a canonical `release_bundle.zip` (reproducible artifact)
- Deploy by selecting a release:
  - switch `current -> releases/<release_name>`
  - restart systemd service
  - verify healthcheck endpoint
- View service status and logs

## Components
- admin_panel/ : Admin web panel (FastAPI + HTML)
- scripts/ : Installation and deployment scripts
- systemd/ : systemd unit templates
- nginx/ : nginx reverse proxy templates
- infra/ : live CT configuration snapshots (systemd/nginx/sudoers)
- docs/ : specs and documentation
- examples/ : example release bundle structure

## Notes
- `nginx/` and `systemd/` contain generic reusable templates.
- `infra/` contains live snapshots copied from a working CT instance.

## Deployment model
- Only intranet usage
- Strongly recommended to run behind Nginx on port 80
- Service itself runs on an internal port (default 8000)

## Status
MVP in progress.
