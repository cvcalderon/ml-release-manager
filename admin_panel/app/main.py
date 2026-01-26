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
import importlib.metadata
import urllib.request
import shutil
import subprocess
import uuid
import zipfile
import os
import threading
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

RUNTIME_BASE_DEPS = {
    "fastapi": ["fastapi", "uvicorn"],
}

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
        return RedirectResponse(url="/admin/install", status_code=303)

    if (RELEASES / rname).exists():
        # refuse overwrite for MVP
        return RedirectResponse(url="/admin/install", status_code=303)

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
        return RedirectResponse(url="/admin/install", status_code=303)

    # validate structure (MVP: even invalid releases are installed for debugging)
    ok, errors = validate_zip_structure(tmp_dir)

    # ensure release.json created/filled
    ensure_release_json(tmp_dir, rname, description, created_by, api_port)

    # install release
    dest = RELEASES / rname
    shutil.move(str(tmp_dir), str(dest))

    # unified validation (structure + deps + compile/import)
    validate_release(dest)

    # build canonical zip
    build_canonical_zip(dest)

    # cleanup upload zip
    zip_path.unlink(missing_ok=True)

    return RedirectResponse(url="/admin/install", status_code=303)


@app.post("/admin/deploy/{release_name}")
def deploy_release(release_name: str):

    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin", status_code=303)

    # ✅ Ensure /opt/release_manager exists (parent of CURRENT)
    CURRENT.parent.mkdir(parents=True, exist_ok=True)

    # ✅ BLOCK deploy if release is not VALID (must be validated first)
    vpath = target / "validation_report.json"
    is_valid = False

    if vpath.exists():
        try:
            vj = json.loads(vpath.read_text(encoding="utf-8"))
            is_valid = (vj.get("ok") is True)
        except Exception:
            is_valid = False

    if not is_valid:
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)

    # ✅ EXTRA GUARD 1: block if per-release venv missing (runtime would crash)
    venv_py = target / ".venv" / "bin" / "python"
    if not venv_py.exists():
        (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
        (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
            f"Deploy blocked: missing per-release venv for release={rname}\n",
            encoding="utf-8"
        )
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)

    # ✅ EXTRA GUARD 2: block if missing deps in release venv
    # Requires Fix2+Fix3 helpers:
    # - get_required_pip_requirements()
    # - check_missing_in_release_venv()
    try:
        required = get_required_pip_requirements(target)
        missing = check_missing_in_release_venv(target, required)
    except Exception as e:
        (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
        (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
            f"Deploy blocked: dependency check error for release={rname}\nerror={str(e)}\n",
            encoding="utf-8"
        )
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)

    if missing:
        (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
        (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
            f"Deploy blocked: missing dependencies for release={rname}: {', '.join(missing)}\n",
            encoding="utf-8"
        )
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
            if prev_target and prev_target.exists():
                if CURRENT.exists() or CURRENT.is_symlink():
                    if CURRENT.is_dir() and not CURRENT.is_symlink():
                        shutil.rmtree(CURRENT)
                    else:
                        CURRENT.unlink(missing_ok=True)

                CURRENT.symlink_to(prev_target)
                run(["sudo", "systemctl", "restart", SERVICE_NAME])

            (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
            (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
                f"Deploy failed for release={rname}\nhealth_path={health_path}\nerror={last_msg}\n",
                encoding="utf-8"
            )

    except Exception as e:
        (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
        (RUNTIME / "logs" / "last_deploy_error.txt").write_text(
            f"Deploy exception for release={rname}\nerror={str(e)}\n",
            encoding="utf-8"
        )
        return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)

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

    # --- validation summary (for UI) ---
    validation_summary = {
        "exists": False,
        "ok": None,
        "timestamp": None,
        "message": "No validation report found yet.",
    }

    if vpath.exists():
        validation_summary["exists"] = True
        try:
            vj = json.loads(vpath.read_text(encoding="utf-8"))
            ok = (vj.get("ok") is True)
            validation_summary["ok"] = ok
            validation_summary["timestamp"] = vj.get("timestamp_utc")

            # Compact message: errors > output > fallback
            if not ok:
                errs = vj.get("errors")
                if isinstance(errs, list) and len(errs) > 0:
                    validation_summary["message"] = " | ".join(str(x) for x in errs[:3])
                else:
                    out = vj.get("output")
                    if isinstance(out, str) and out.strip():
                        validation_summary["message"] = out.strip().splitlines()[-1][:160]
                    else:
                        validation_summary["message"] = "Validation failed (no details)."
            else:
                validation_summary["message"] = "Validation OK."
        except Exception:
            validation_summary["ok"] = False
            validation_summary["message"] = "Validation report exists but could not be parsed."


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
            "validation_summary": validation_summary,
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

@app.post("/admin/release/{release_name}/install_missing_deps")
def install_missing_deps(release_name: str):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return RedirectResponse(url="/admin/install", status_code=303)

    # safety: do not modify deps while ACTIVE
    if CURRENT.exists():
        try:
            if CURRENT.resolve() == target.resolve():
                return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)
        except Exception:
            pass

    # start background thread (simple MVP)
    def _worker():
        ok, out = install_missing_deps_with_progress(target)
        if not ok:
            # keep pip output in validation report for visibility
            write_validation_report(target, False, {"errors": ["Dependency installation failed"], "pip_output": out[-1200:]})
        # re-validate after install attempt
        validate_release(target)

    threading.Thread(target=_worker, daemon=True).start()
    return RedirectResponse(url=f"/admin/release/{rname}", status_code=303)


@app.get("/admin/release/{release_name}/deps_status")
def deps_status(release_name: str):
    rname = safe_name(release_name)
    target = RELEASES / rname
    if not target.exists():
        return {"status": "missing", "progress": 0, "message": "Release not found"}
    return read_deps_progress(target)



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

def check_missing_in_release_venv(release_path: Path, pip_requirements: list[str]) -> list[str]:
    """
    Returns missing requirements *as requirement strings* (not only names).
    Compares canonical names vs installed distributions.
    """
    if not pip_requirements:
        return []

    py = release_venv_python(release_path)
    if not py.exists():
        # if venv doesn't exist, everything is missing
        return pip_requirements[:]  # keep full req strings

    # Ask the venv python what distributions exist
    cmd = (
        "import importlib.metadata as m;"
        "print('\\n'.join([(d.metadata.get('Name') or '').strip() for d in m.distributions()]))"
    )
    rc, out = run([str(py), "-c", cmd])
    if rc != 0:
        return pip_requirements[:]

    installed = set()
    for line in out.splitlines():
        name = line.strip()
        if name:
            installed.add(canonicalize_name(name))

    missing = []
    for req in pip_requirements:
        pkg = req_to_pkg(req)
        if pkg and pkg not in installed:
            missing.append(req)  # keep original req string

    # unique ordered
    seen = set()
    ordered = []
    for r in missing:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


def validate_release(release_path: Path) -> tuple[bool, str]:
    """
    Validates a release (per-release venv):
    - checks service/app.py + service/__init__.py exist
    - ensures <release>/.venv exists (fast)
    - checks missing deps in that venv (does NOT install)
    - if deps missing -> NOT_VALIDATED
    - if deps OK -> compile/import using release venv
    """
    errors: list[str] = []
    details: dict = {}

    # 1) Required files
    app_py = release_path / "service" / "app.py"
    init_py = release_path / "service" / "__init__.py"

    if not init_py.exists():
        errors.append("Missing service/__init__.py")
    if not app_py.exists():
        errors.append("Missing service/app.py")

    if errors:
        write_validation_report(release_path, False, {"errors": errors})
        return False, "\n".join(errors)

    # 2) Ensure per-release venv exists (no deps install here)
    ok_venv, out_venv = ensure_release_venv(release_path)
    details["venv"] = out_venv
    if not ok_venv:
        errors.append("Failed to create per-release venv")
        write_validation_report(release_path, False, {"errors": errors, **details})
        return False, "venv create failed"

    py = release_venv_python(release_path)
    if not py.exists():
        errors.append("Release venv python not found")
        write_validation_report(release_path, False, {"errors": errors, **details})
        return False, "venv python missing"

    # 3) Dependency check (no install)
    #pip_reqs = get_release_pip_requirements(release_path)
    pip_reqs = get_required_pip_requirements(release_path)
    details["pip_requirements"] = pip_reqs

    missing = check_missing_in_release_venv(release_path, pip_reqs)
    details["missing_deps"] = missing
    if missing:
        msg = f"Missing dependencies ({len(missing)}): {', '.join(missing)}"
        write_validation_report(release_path, False, {"errors": [msg], **details})
        return False, msg

    # 4) Compile inside per-release venv
    rc1, out1 = run([str(py), "-m", "py_compile", "service/app.py"], cwd=str(release_path))
    details["py_compile"] = (out1 or "")[-1200:]
    if rc1 != 0:
        errors.append("py_compile failed")
        write_validation_report(release_path, False, {"errors": errors, **details})
        return False, out1

    # 5) Import inside per-release venv
    rc2, out2 = run([str(py), "-c", "import service.app"], cwd=str(release_path))
    details["import_test"] = (out2 or "")[-1200:]
    if rc2 != 0:
        errors.append("import service.app failed")
        write_validation_report(release_path, False, {"errors": errors, **details})
        return False, out2

    # ✅ Success
    write_validation_report(release_path, True, {"errors": [], **details})
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

def _normalize_pkg_name(req: str) -> str:
    """
    Extracts a base package name from a pip requirement string.
    Examples:
      'uvicorn[standard]>=0.23' -> 'uvicorn'
      'lightgbm==4.3.0' -> 'lightgbm'
      'pydantic' -> 'pydantic'
    """
    s = req.strip()
    # remove version operators
    s = re.split(r"(==|>=|<=|~=|!=|>|<)", s)[0]
    # remove extras: package[extra]
    s = s.split("[")[0]
    return s.strip().lower()

def get_release_pip_requirements(release_path: Path) -> list[str]:
    rj_path = release_path / "release.json"
    if not rj_path.exists():
        return []
    try:
        rj = json.loads(rj_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps = rj.get("dependencies", {})
    pip_reqs = deps.get("pip", [])
    if isinstance(pip_reqs, list):
        return [str(x).strip() for x in pip_reqs if str(x).strip()]
    return []

def check_missing_dependencies(pip_requirements: list[str]) -> list[str]:
    """
    Returns a list of missing package names based on importlib.metadata.
    (MVP check: presence only, not version constraint enforcement.)
    """
    if not pip_requirements:
        return []

    # Build installed distribution names (normalized)
    installed = set()
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed.add(name.strip().lower())

    missing = []
    for req in pip_requirements:
        pkg = _normalize_pkg_name(req)
        if pkg and pkg not in installed:
            missing.append(pkg)

    # unique but stable order
    seen = set()
    ordered = []
    for x in missing:
        if x not in seen:
            seen.add(x)
            ordered.append(x)
    return ordered

def release_venv_python(release_path: Path) -> Path:
    return release_path / ".venv" / "bin" / "python"


def ensure_release_venv(release_path: Path) -> tuple[bool, str]:
    """
    Ensure a per-release venv exists at <release_path>/.venv
    Returns (ok, output).
    """
    py = release_venv_python(release_path)

    # already exists
    if py.exists():
        return True, "venv exists"

    # create venv
    rc, out = run(["python3", "-m", "venv", str(release_path / ".venv")])
    if rc != 0:
        return False, out

    # upgrade pip tooling (recommended)
    rc2, out2 = run([str(py), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    if rc2 != 0:
        return False, out + "\n" + out2

    return True, out2

def pip_install_in_release_venv(release_path: Path, pip_requirements: list[str]) -> tuple[bool, str]:
    """
    Install pip requirements inside the per-release venv.
    """
    if not pip_requirements:
        return True, "no dependencies"

    py = release_venv_python(release_path)
    cmd = [str(py), "-m", "pip", "install"] + pip_requirements
    rc, out = run(cmd)
    return (rc == 0), out


def install_missing_deps_with_progress(release_path: Path) -> tuple[bool, str]:
    # ✅ Ensure per-release venv exists first
    ok_venv, msg_venv = ensure_release_venv(release_path)
    if not ok_venv:
        write_deps_progress(release_path, {"status": "error", "progress": 0, "message": f"Venv error: {msg_venv}"})
        return False, msg_venv

    pip_reqs = get_required_pip_requirements(release_path)
    missing = check_missing_in_release_venv(release_path, pip_reqs)

    if not missing:
        write_deps_progress(release_path, {"status": "done", "progress": 100, "message": "No missing dependencies"})
        return True, "No missing dependencies"

    total = len(missing)
    ok_all = True
    combined_out = ""

    write_deps_progress(release_path, {"status": "running", "progress": 0, "message": f"Installing {total} packages..."})

    py = release_venv_python(release_path)

    for i, req in enumerate(missing, start=1):
        pct = int((i / total) * 100)
        write_deps_progress(
            release_path,
            {"status": "running", "progress": pct, "message": f"Installing {req} ({i}/{total})..."}
        )

        rc, out = run([str(py), "-m", "pip", "install", req], cwd=str(release_path))
        combined_out += f"\n--- {req} ---\n{out}\n"

        if rc != 0:
            ok_all = False
            write_deps_progress(
                release_path,
                {"status": "error", "progress": pct, "message": f"Failed installing {req}"}
            )
            break

    if ok_all:
        write_deps_progress(release_path, {"status": "done", "progress": 100, "message": "Dependencies installed"})
        return True, combined_out

    return False, combined_out


def deps_progress_path(release_path: Path) -> Path:
    return release_path / ".deps_install_progress.json"


def write_deps_progress(release_path: Path, data: dict) -> None:
    deps_progress_path(release_path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_deps_progress(release_path: Path) -> dict:
    p = deps_progress_path(release_path)
    if not p.exists():
        return {"status": "idle", "progress": 0, "message": ""}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle", "progress": 0, "message": ""}


def get_release_service_type(release_path: Path) -> str:
    rj = release_path / "release.json"
    if not rj.exists():
        return "fastapi"
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
        return (data.get("service_type") or "fastapi").lower()
    except Exception:
        return "fastapi"

def get_required_pip_requirements(release_path: Path) -> list[str]:
    service_type = get_release_service_type(release_path)
    base = RUNTIME_BASE_DEPS.get(service_type, [])
    user = get_release_pip_requirements(release_path)  # tu función actual
    return base + user

def get_release_python(release_path: Path) -> Path:
    return release_path / ".venv" / "bin" / "python"


from packaging.utils import canonicalize_name
from packaging.requirements import Requirement

def req_to_pkg(req: str) -> str:
    """
    Extract canonical package name from a requirement string.
    Handles versions and extras: uvicorn[standard]>=0.25 -> uvicorn
    """
    try:
        r = Requirement(req)
        return canonicalize_name(r.name)
    except Exception:
        # Fallback simple
        x = req.strip()
        if not x:
            return ""
        # strip version constraints
        for sep in ["==", ">=", "<=", ">", "<", "~=", "!="]:
            if sep in x:
                x = x.split(sep, 1)[0].strip()
        # strip extras
        if "[" in x:
            x = x.split("[", 1)[0].strip()
        return canonicalize_name(x)
