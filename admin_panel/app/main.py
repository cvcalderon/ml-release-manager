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
import re
import sys
import json
import time
import urllib.request
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
SERVICE_PORT = 8000
DEFAULT_HEALTH_PATH = "/health"
HEALTH_TIMEOUT_SEC = 8


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

@app.get("/admin")
@app.get("/admin/")
def admin_root():
    return RedirectResponse(url="/admin/install", status_code=303)

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

    #write_validation_report(dest, ok, errors, details)
    write_validation_report(dest, ok, {"errors": errors, **details})
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

    # --- BLOCK DEPLOY IF NOT VALID ---
    vpath = target / "validation_report.json"
    is_valid = False

    if vpath.exists():
        try:
            vj = json.loads(vpath.read_text(encoding="utf-8"))
            is_valid = (vj.get("ok") is True)
        except Exception:
            is_valid = False

    if not is_valid:
        # Redirect to release detail (user must validate first)
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)


    # 1) Remember previous active release (if any)
    prev_target = None
    if CURRENT.exists():
        try:
            prev_target = CURRENT.resolve()
        except Exception:
            prev_target = None

    try:
        # 2) Remove current (symlink or dir)
        if CURRENT.exists() or CURRENT.is_symlink():
            if CURRENT.is_dir() and not CURRENT.is_symlink():
                shutil.rmtree(CURRENT)
            else:
                CURRENT.unlink(missing_ok=True)

        # 3) Set current -> new release
        CURRENT.symlink_to(target)

        # 4) Restart service
        run(["sudo", "systemctl", "restart", SERVICE_NAME])

        # 5) Healthcheck loop (a few tries)
        health_path = get_health_path(target)
        ok = False
        last_msg = ""
        for _ in range(3):
            ok, last_msg = http_healthcheck(SERVICE_PORT, health_path, timeout_sec=HEALTH_TIMEOUT_SEC)
            if ok:
                break
            time.sleep(1)

        # 6) Rollback if failed
        if not ok:
            # restore previous
            if prev_target and prev_target.exists():
                if CURRENT.exists() or CURRENT.is_symlink():
                    if CURRENT.is_dir() and not CURRENT.is_symlink():
                        shutil.rmtree(CURRENT)
                    else:
                        CURRENT.unlink(missing_ok=True)
                CURRENT.symlink_to(prev_target)
                run(["sudo", "systemctl", "restart", SERVICE_NAME])

            # Optional: store last deploy error (simple file)
            (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
            (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
                f"Deploy failed for release={rname}\nhealth_path={health_path}\nerror={last_msg}\n",
                encoding="utf-8"
            )

    except Exception:
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

@app.get("/admin/release/{release_name}")
def release_detail_page(release_name: str, request: Request):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin/releases", status_code=303)

    # ACTIVE?
    active = False
    if CURRENT.exists():
        try:
            active = CURRENT.resolve() == target.resolve()
        except Exception:
            active = False

    # read release.json for description
    rj_path = target / "release.json"
    release_json_text = rj_path.read_text(encoding="utf-8") if rj_path.exists() else "{}"
    try:
        rj = json.loads(release_json_text)
    except Exception:
        rj = {}

    api_cfg = rj.get("api", {})
    base_path = api_cfg.get("base_path", "/api")
    docs_path = api_cfg.get("docs_path", "/docs")

    # sanitize
    if not base_path.startswith("/"):
        base_path = "/" + base_path
    if base_path.endswith("/"):
        base_path = base_path[:-1]

    if not docs_path.startswith("/"):
        docs_path = "/" + docs_path

    api_url = f"{base_path}{docs_path}"


    description = rj.get("description", "")

    # determine validation status
    status = "NOT_VALIDATED"
    vpath = target / "validation_report.json"
    if vpath.exists():
        try:
            vj = json.loads(vpath.read_text(encoding="utf-8"))
            status = "VALID" if vj.get("ok") is True else "NOT_VALIDATED"
        except Exception:
            status = "NOT_VALIDATED"

    if active:
        status = "ACTIVE"

    # read main "service/app.py"
    main_path = target / "service" / "app.py"
    main_py = main_path.read_text(encoding="utf-8") if main_path.exists() else ""

    # get service status + logs
    service_status = service_status_text()
    service_logs = service_logs_text()

    r = {"name": rname, "status": status, "description": description}

    return templates.TemplateResponse(
        "release_detail.html",
        {
            "request": request,
            "nav": "releases",
            "page_title": "Release Detail",
            "page_subtitle": "Operate and manage this release safely",
            "r": r,
            "main_py": main_py,
            "release_json_text": release_json_text,
            "service_status": service_status,
            "service_logs": service_logs,
            "api_url": api_url,
        },
    )

@app.post("/admin/release/{release_name}/clone")
def clone_release(release_name: str):
    rname = safe_name(release_name)
    src = RELEASES / rname
    if not src.exists():
        return RedirectResponse(url="/admin/releases", status_code=303)

    new_name = next_copy_name(rname)
    dst = RELEASES / new_name

    shutil.copytree(src, dst)

    # mark as NOT_VALIDATED by default (optional)
    write_validation_report(dst, False, {"errors": ["Cloned release (requires validation after edits)"]})

    return RedirectResponse(url=f"/admin/release/{new_name}", status_code=303)


@app.post("/admin/release/{release_name}/update_main")
def update_main(release_name: str, content: str = Form(...)):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin/releases", status_code=303)

    # block update if ACTIVE
    if CURRENT.exists():
        try:
            if CURRENT.resolve() == target.resolve():
                return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)
        except Exception:
            pass

    main_path = target / "service" / "app.py"
    main_path.write_text(content, encoding="utf-8")

    validate_release(target)
    return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)


@app.post("/admin/release/{release_name}/update_release_json")
def update_release_json(release_name: str, content: str = Form(...)):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin/releases", status_code=303)

    # block update if ACTIVE
    if CURRENT.exists():
        try:
            if CURRENT.resolve() == target.resolve():
                return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)
        except Exception:
            pass

    # basic JSON validation
    try:
        json.loads(content)
    except Exception:
        write_validation_report(target, False, {"errors": ["release.json is not valid JSON"]})
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)

    (target / "release.json").write_text(content, encoding="utf-8")

    validate_release(target)
    return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)


@app.get("/admin/install")
def install_page(request: Request):
    releases = list_releases()  # usa tu función actual
    return templates.TemplateResponse(
        "install.html",
        {
            "request": request,
            "title": "Install Release",
            "page_title": "Install Release",
            "page_subtitle": "Upload and manage installed release bundles",
            "nav": "install",
            "releases": releases,
        },
    )

@app.get("/admin/releases")
def releases_page(request: Request):
    releases = list_releases()
    return templates.TemplateResponse(
        "releases.html",
        {
            "request": request,
            "title": "Releases",
            "page_title": "Releases",
            "page_subtitle": "Browse all installed releases",
            "nav": "releases",
            "releases": releases,
        },
    )



# Helpers

def http_healthcheck(port: int, path: str, timeout_sec: int = 8) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            if 200 <= resp.status < 300:
                return True, body[:500]
            return False, f"HTTP {resp.status}: {body[:200]}"
    except Exception as e:
        return False, str(e)

def load_release_json(release_path: Path) -> dict:
    p = release_path / "release.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def get_health_path(release_path: Path) -> str:
    rj = load_release_json(release_path)
    hc = rj.get("healthcheck", {})
    path = hc.get("path") or DEFAULT_HEALTH_PATH
    if not path.startswith("/"):
        path = "/" + path
    return path

def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def list_tree(root: Path, max_depth: int = 2) -> list[str]:
    """
    Simple tree listing (no recursion explosion)
    """
    out = []
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        try:
            rel = p.relative_to(root)
        except Exception:
            continue
        depth = len(rel.parts)
        if depth > max_depth:
            continue
        if p.is_dir():
            out.append(f"{rel.as_posix()}/")
        else:
            out.append(rel.as_posix())
    return out

def service_status_text() -> str:
    rc, out = run(["sudo", "systemctl", "status", SERVICE_NAME, "--no-pager"])
    return out

def service_logs_text() -> str:
    rc, out = run(["sudo", "journalctl", "-u", SERVICE_NAME, "-n", "50", "--no-pager"])
    return out


def write_validation_report(release_path: Path, ok: bool, detail: dict) -> None:
    report = {
        "ok": ok,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        **detail,
    }
    (release_path / "validation_report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

def validate_release(release_path: Path) -> tuple[bool, str]:
    """
    Validates a release after updates:
    - checks service/app.py exists
    - python -m py_compile service/app.py
    - import service.app
    """
    errors = []

    app_py = release_path / "service" / "app.py"
    init_py = release_path / "service" / "__init__.py"
    if not init_py.exists():
        errors.append("Missing service/__init__.py")
    if not app_py.exists():
        errors.append("Missing service/app.py")

    if errors:
        write_validation_report(release_path, False, {"errors": errors})
        return False, "\n".join(errors)

    py = sys.executable  # uses the venv python running the admin panel

    # 1) compile
    rc1, out1 = run([py, "-m", "py_compile", "service/app.py"], cwd=str(release_path))
    if rc1 != 0:
        write_validation_report(release_path, False, {"errors": ["py_compile failed"], "output": out1})
        return False, out1

    # 2) import
    rc2, out2 = run([py, "-c", "import service.app"], cwd=str(release_path))
    if rc2 != 0:
        write_validation_report(release_path, False, {"errors": ["import service.app failed"], "output": out2})
        return False, out2

    write_validation_report(release_path, True, {"output": out1 + "\n" + out2})
    return True, "OK"

def next_copy_name(base_name: str) -> str:
    prefix = f"{base_name}_copy_"
    max_n = 0
    if RELEASES.exists():
        for p in RELEASES.iterdir():
            if p.is_dir() and p.name.startswith(prefix):
                m = re.match(rf"^{re.escape(prefix)}(\d+)$", p.name)
                if m:
                    max_n = max(max_n, int(m.group(1)))
    return f"{prefix}{max_n+1:03d}"
