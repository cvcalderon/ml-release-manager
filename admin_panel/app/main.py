"""
Release Manager - Admin Panel (MVP)
-----------------------------------
Intranet panel to:
- Upload a release ZIP
- Validate basic structure
- Install into /opt/release_manager/releases/<release_name>/
- Create canonical release_bundle.zip
- Deploy by switching /opt/release_manager/current symlink
- Restart systemd service "release-service"
- Download installed release_bundle.zip
- View service status and logs

This MVP uses a minimal HTML UI (Jinja2 templates).
"""

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import json
import shutil
import subprocess
import uuid
import zipfile
import os
from datetime import datetime, timezone

BASE = Path("/opt/release_manager")
RELEASES = BASE / "releases"
CURRENT = BASE / "current"
UPLOADS = BASE / "runtime" / "uploads"
VENV = BASE / "venv" / "bin" / "python"
SERVICE_NAME = "ml-release-service"

app = FastAPI(title="Release Manager - Admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    return p.returncode, p.stdout


def safe_name(name: str) -> str:
    # allow only safe folder names
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return "".join([c for c in name if c in allowed]).strip("_-")

def list_releases():
    items = []
    if RELEASES.exists():
        for d in sorted(RELEASES.iterdir()):
            if d.is_dir():
                meta = d / "release.json"
                status = "UNKNOWN"
                if (d / "validation_report.json").exists():
                    rep = json.loads((d / "validation_report.json").read_text())
                    status = "VALID" if rep.get("ok") else "INVALID"
                active = CURRENT.resolve() == d.resolve() if CURRENT.exists() else False
                items.append({
                    "name": d.name,
                    "path": str(d),
                    "status": "ACTIVE" if active else status,
                    "has_bundle": (d / "release_bundle.zip").exists(),
                    "meta": json.loads(meta.read_text()) if meta.exists() else {}
                })
    return items

def validate_zip_structure(tmp_dir: Path) -> tuple[bool, list[str]]:
    errors = []
    if not (tmp_dir / "service").exists():
        errors.append("Missing 'service/' directory")
    if not (tmp_dir / "assets").exists():
        errors.append("Missing 'assets/' directory")
    # release.json optional at upload time
    return (len(errors) == 0, errors)

def write_validation_report(dest: Path, ok: bool, errors: list[str], details: dict):
    report = {
        "ok": ok,
        "errors": errors,
        "details": details,
        "validated_at": datetime.now(timezone.utc).isoformat()
    }
    (dest / "validation_report.json").write_text(json.dumps(report, indent=2))

def ensure_release_json(tmp_dir: Path, release_name: str, description: str, created_by: str, api_port: int):
    p = tmp_dir / "release.json"
    if p.exists():
        data = json.loads(p.read_text())
    else:
        data = {}

    # Fill minimal required fields
    data["release_name"] = data.get("release_name", release_name)
    data["project_name"] = data.get("project_name", "generic-service")
    data["service_type"] = data.get("service_type", "fastapi")
    data["entrypoint"] = data.get("entrypoint", "service.app:app")
    data["api_port"] = int(data.get("api_port", api_port))
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["created_by"] = created_by
    data["description"] = data.get("description", description)
    data["healthcheck"] = data.get("healthcheck", {"path": "/health", "method": "GET"})
    p.write_text(json.dumps(data, indent=2))
    return data

def build_canonical_zip(release_dir: Path):
    out_zip = release_dir / "release_bundle.zip"
    if out_zip.exists():
        out_zip.unlink()

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(release_dir):
            for f in files:
                fp = Path(root) / f
                rel = fp.relative_to(release_dir)
                # do not include itself recursively
                if rel.name == "release_bundle.zip":
                    continue
                z.write(fp, str(rel))
    return out_zip

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/admin/", status_code=302)

@app.get("/admin/", response_class=HTMLResponse)
def home_slash(request: Request):
    releases = list_releases()
    return templates.TemplateResponse("index.html", {"request": request, "releases": releases})


@app.get("/admin", response_class=HTMLResponse)
def home(request: Request):
    releases = list_releases()
    return templates.TemplateResponse("index.html", {"request": request, "releases": releases})

@app.post("/admin/upload")
def upload_release(
    file: UploadFile = File(...),
    release_name: str = Form(...),
    description: str = Form(""),
    api_port: int = Form(8000),
    created_by: str = Form("admin")
):
    rname = safe_name(release_name)
    if not rname:
        return RedirectResponse(url="/admin", status_code=303)

    if (RELEASES / rname).exists():
        # refuse overwrite for MVP
        return RedirectResponse(url="/admin", status_code=303)

    UPLOADS.mkdir(parents=True, exist_ok=True)
    tmp_id = uuid.uuid4().hex
    zip_path = UPLOADS / f"{tmp_id}.zip"
    tmp_dir = UPLOADS / f"tmp_{tmp_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with zip_path.open("wb") as f:
        f.write(file.file.read())

    # extract
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        zip_path.unlink(missing_ok=True)
        return RedirectResponse(url="/admin", status_code=303)

    ok, errors = validate_zip_structure(tmp_dir)

    # ensure release.json created/filled
    meta = ensure_release_json(tmp_dir, rname, description, created_by, api_port)

    # basic import test (universal validation)
    details = {"release.json": meta}
    if ok:
        # compile + import entrypoint
        # expects: service/app.py and entrypoint service.app:app
        rc1, out1 = run([str(VENV), "-m", "py_compile", "service/app.py"], cwd=tmp_dir)
        rc2, out2 = run([str(VENV), "-c", "from service.app import app"], cwd=tmp_dir)
        details["py_compile"] = out1
        details["import_test"] = out2
        if rc1 != 0 or rc2 != 0:
            ok = False
            errors.append("Python compile/import validation failed")

    # install release (even if invalid -> keep for debugging)
    dest = RELEASES / rname
    shutil.move(str(tmp_dir), str(dest))

    write_validation_report(dest, ok, errors, details)
    build_canonical_zip(dest)

    # cleanup upload zip
    zip_path.unlink(missing_ok=True)

    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/deploy/{release_name}")
def deploy_release(release_name: str):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin", status_code=303)

    try:
        # 1) Remove current whether it's a symlink or directory
        if CURRENT.is_symlink() or CURRENT.exists():
            if CURRENT.is_dir() and not CURRENT.is_symlink():
                shutil.rmtree(CURRENT)
            else:
                CURRENT.unlink(missing_ok=True)

        # 2) Create symlink
        CURRENT.symlink_to(target)

        # 3) Restart deployed service
        #run(["systemctl", "restart", SERVICE_NAME])
        run(["sudo", "systemctl", "restart", SERVICE_NAME])

    except Exception as e:
        # log error to runtime logs (optional)
        return RedirectResponse(url="/admin", status_code=303)

    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/deactivate")
def deactivate_service():
    """
    Stop deployed service and remove the current symlink.
    This allows deleting an ACTIVE release safely from the GUI.
    """
    try:
        # Stop service (no password via sudoers)
        run(["sudo", "systemctl", "stop", SERVICE_NAME])

        # Remove current (symlink or directory)
        if CURRENT.exists() or CURRENT.is_symlink():
            if CURRENT.is_dir() and not CURRENT.is_symlink():
                shutil.rmtree(CURRENT)
            else:
                CURRENT.unlink(missing_ok=True)

    except Exception:
        return RedirectResponse(url="/admin", status_code=303)

    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/download/{release_name}")
def download_release(release_name: str):
    rname = safe_name(release_name)
    target = RELEASES / rname
    bundle = target / "release_bundle.zip"
    if not bundle.exists():
        build_canonical_zip(target)
    return FileResponse(path=bundle, filename=f"{rname}_release_bundle.zip")

@app.post("/admin/delete/{release_name}")
def delete_release(release_name: str):
    rname = safe_name(release_name)
    target = RELEASES / rname
    # do not delete active
    if CURRENT.exists() and CURRENT.resolve() == target.resolve():
        return RedirectResponse(url="/admin", status_code=303)

    shutil.rmtree(target, ignore_errors=True)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/service_status", response_class=HTMLResponse)
def service_status(request: Request):
    rc, out = run(["systemctl", "status", SERVICE_NAME, "--no-pager"])
    return templates.TemplateResponse("service.html", {"request": request, "status": out})

@app.get("/admin/logs", response_class=HTMLResponse)
def logs(request: Request):
    rc, out = run(["journalctl", "-u", SERVICE_NAME, "-n", "200", "--no-pager"])
    return templates.TemplateResponse("logs.html", {"request": request, "logs": out})
