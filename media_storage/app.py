import hashlib
import hmac
import html
import io
import mimetypes
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, ImageOps

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/heic", ".heic")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_STORAGE_BYTES = int(os.environ.get("MAX_STORAGE_BYTES", str(50 * 1024 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))  # 100 MB per single file
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://storage.dario10.pw").rstrip("/")
STORAGE_API_KEY = os.environ.get("STORAGE_API_KEY", "").strip()

STORAGE_WEB_USER = os.environ.get("STORAGE_WEB_USER", "dario").strip() or "dario"
STORAGE_WEB_PASS = os.environ.get("STORAGE_WEB_PASS", "tuxx6393")
_SESSION_SECRET = (
    os.environ.get("STORAGE_SESSION_SECRET", "").strip()
    or STORAGE_API_KEY
    or "dms-local-fallback-secret-change-me"
)
SESSION_COOKIE_NAME = "dms_session"
SESSION_TTL_SECONDS = 14 * 24 * 3600

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
SAFE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")

_quota_lock = threading.Lock()
_cached_used_bytes: int | None = None

app = FastAPI(title="Dario Media Storage", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def _make_session_token(username: str) -> str:
    exp = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}.{exp}"
    sig = hmac.new(_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: str | None) -> str | None:
    if not token or token.count(".") != 2:
        return None
    username, exp_str, sig = token.rsplit(".", 2)
    try:
        exp = int(exp_str)
    except ValueError:
        return None
    if exp < int(time.time()):
        return None
    payload = f"{username}.{exp_str}"
    expected = hmac.new(_SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if not hmac.compare_digest(username, STORAGE_WEB_USER):
        return None
    return username


def _get_session_user(request: Request) -> str | None:
    return _verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def _scan_usage() -> tuple[int, dict[str, dict[str, int]]]:
    total = 0
    buckets: dict[str, dict[str, int]] = {}
    if not DATA_DIR.exists():
        return 0, buckets

    for root, _, files in os.walk(DATA_DIR):
        root_path = Path(root)
        try:
            rel = root_path.relative_to(DATA_DIR)
            bucket_name = rel.parts[0] if rel.parts else "root"
        except ValueError:
            bucket_name = "root"
        if bucket_name == "lost+found":
            continue
        b_entry = buckets.setdefault(bucket_name, {"bytes": 0, "files": 0})
        for fname in files:
            fpath = root_path / fname
            try:
                sz = fpath.stat().st_size
            except OSError:
                continue
            total += sz
            b_entry["bytes"] += sz
            b_entry["files"] += 1
    return total, buckets


def _get_used_bytes() -> int:
    global _cached_used_bytes
    with _quota_lock:
        if _cached_used_bytes is None:
            _cached_used_bytes, _ = _scan_usage()
        return _cached_used_bytes


def _quota_summary(used_bytes: int | None = None) -> dict:
    used = _get_used_bytes() if used_bytes is None else used_bytes
    free = max(0, MAX_STORAGE_BYTES - used)
    pct = round((used / MAX_STORAGE_BYTES) * 100, 2) if MAX_STORAGE_BYTES > 0 else 100.0
    return {
        "usedBytes": used,
        "maxBytes": MAX_STORAGE_BYTES,
        "freeBytes": free,
        "usedPercent": pct,
        "maxGigabytes": round(MAX_STORAGE_BYTES / (1024**3), 2),
        "usedGigabytes": round(used / (1024**3), 4),
    }


def _check_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> bool:
    if not STORAGE_API_KEY:
        return False
    candidate = (x_api_key or "").strip()
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not candidate and api_key:
        candidate = api_key.strip()
    return bool(candidate and hmac.compare_digest(candidate, STORAGE_API_KEY))


def _require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> None:
    if _get_session_user(request):
        return
    if _check_api_key(x_api_key=x_api_key, authorization=authorization, api_key=api_key):
        return
    raise HTTPException(status_code=401, detail="Authentication required.")


def _sanitize_project(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", (raw or "general").strip().lower()).strip("-")
    if not slug or not BUCKET_RE.match(slug):
        slug = "general"
    return slug[:48]


def _resolve_key_path(key: str) -> Path:
    clean = (key or "").lstrip("/").replace("\\", "/")
    if not clean or ".." in clean.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file key.")
    target = (DATA_DIR / clean).resolve()
    if not target.is_relative_to(DATA_DIR) or target == DATA_DIR:
        raise HTTPException(status_code=400, detail="Invalid file path.")
    return target


def _maybe_optimize_image(
    raw_bytes: bytes,
    orig_ext: str,
    max_width: int,
    quality: int,
) -> tuple[bytes, str, str]:
    """Optionally downscale and convert large raster images to WebP."""
    if orig_ext in {".gif", ".svg"}:
        return raw_bytes, orig_ext, mimetypes.types_map.get(orig_ext, "application/octet-stream")

    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            w, h = img.size
            if max_width > 0 and max(w, h) > max_width:
                ratio = max_width / float(max(w, h))
                img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.Resampling.LANCZOS)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=max(40, min(95, quality)), method=4)
            optimized = out.getvalue()
            if len(optimized) < len(raw_bytes) or orig_ext not in {".webp"}:
                return optimized, ".webp", "image/webp"
    except Exception:
        pass
    mime = mimetypes.guess_type(f"x{orig_ext}")[0] or "application/octet-stream"
    return raw_bytes, orig_ext, mime


@app.post("/auth/login")
async def auth_login(request: Request, response: Response):
    content_type = (request.headers.get("content-type") or "").lower()
    username = ""
    password = ""
    if "application/json" in content_type:
        data = await request.json()
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
    else:
        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")

    user_ok = hmac.compare_digest(username, STORAGE_WEB_USER)
    pass_ok = hmac.compare_digest(password, STORAGE_WEB_PASS)
    if not (user_ok and pass_ok):
        return JSONResponse(
            {"ok": False, "detail": "Usuario o contraseña incorrectos."},
            status_code=401,
        )

    token = _make_session_token(STORAGE_WEB_USER)
    resp = JSONResponse({"ok": True, "username": STORAGE_WEB_USER})
    is_https = PUBLIC_BASE_URL.startswith("https://") or request.url.scheme == "https"
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    return resp


@app.post("/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return resp


@app.get("/healthz")
def healthz():
    used = _get_used_bytes()
    return {
        "ok": True,
        "service": "dario-media-storage",
        "quota": _quota_summary(used),
    }


@app.get("/api/stats", dependencies=[Depends(_require_auth)])
def api_stats():
    global _cached_used_bytes
    total, buckets = _scan_usage()
    with _quota_lock:
        _cached_used_bytes = total
    return {
        "ok": True,
        "service": "dario-media-storage",
        "dataDir": str(DATA_DIR),
        "quota": _quota_summary(total),
        "buckets": [
            {
                "project": name,
                "files": info["files"],
                "usedBytes": info["bytes"],
                "usedMegabytes": round(info["bytes"] / (1024 * 1024), 2),
            }
            for name, info in sorted(buckets.items())
        ],
    }


@app.get("/api/files", dependencies=[Depends(_require_auth)])
def api_list_files(
    project: str = "",
    prefix: str = "",
    search: str = "",
    limit: int = 250,
):
    target_bucket = _sanitize_project(project) if project else ""
    clean_prefix = prefix.strip("/").replace("\\", "/") if prefix else ""
    q = search.strip().lower()
    items = []
    folders_map: dict[str, dict[str, int]] = {}

    if target_bucket:
        search_root = DATA_DIR / target_bucket
    elif clean_prefix:
        search_root = _resolve_key_path(clean_prefix)
    else:
        search_root = DATA_DIR

    if search_root.exists():
        for root, _, files in os.walk(search_root):
            root_path = Path(root)
            if "lost+found" in root_path.parts:
                continue
            for fname in files:
                fpath = root_path / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                rel_key = fpath.relative_to(DATA_DIR).as_posix()
                parts = rel_key.split("/")
                bucket_name = parts[0] if len(parts) > 1 else "root"
                folder_rel = "/".join(parts[:-1]) if len(parts) > 1 else ""
                if folder_rel:
                    f_stat = folders_map.setdefault(folder_rel, {"files": 0, "bytes": 0})
                    f_stat["files"] += 1
                    f_stat["bytes"] += st.st_size
                if clean_prefix and not (rel_key == clean_prefix or rel_key.startswith(clean_prefix + "/")):
                    continue
                if q and q not in rel_key.lower():
                    continue
                mime_type = mimetypes.guess_type(fname)[0] or "application/octet-stream"
                items.append(
                    {
                        "key": rel_key,
                        "name": fname,
                        "project": bucket_name,
                        "folder": folder_rel,
                        "diskPath": f"/data/{rel_key}",
                        "url": f"{PUBLIC_BASE_URL}/files/{rel_key}",
                        "mimeType": mime_type,
                        "sizeBytes": st.st_size,
                        "modifiedAt": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
    items.sort(key=lambda x: x["modifiedAt"], reverse=True)
    folders = [
        {"path": k, "files": v["files"], "usedBytes": v["bytes"]}
        for k, v in sorted(folders_map.items())
    ]
    return {
        "ok": True,
        "dataDir": str(DATA_DIR),
        "items": items[: max(1, min(limit, 1000))],
        "totalFound": len(items),
        "folders": folders,
        "quota": _quota_summary(),
    }


@app.post("/api/upload", dependencies=[Depends(_require_auth)])
async def api_upload(
    file: UploadFile = File(...),
    project: str = Form("general"),
    optimize: str = Form("1"),
    max_width: int = Form(1800),
    quality: int = Form(82),
):
    global _cached_used_bytes

    bucket = _sanitize_project(project)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds single upload limit ({MAX_UPLOAD_BYTES // (1024 * 1024)} MB).",
        )

    orig_name = file.filename or "file"
    orig_ext = Path(orig_name).suffix.lower()
    if not SAFE_EXT_RE.match(orig_ext):
        guessed = mimetypes.guess_extension(file.content_type or "") or ".bin"
        orig_ext = guessed if SAFE_EXT_RE.match(guessed) else ".bin"

    content_type = (file.content_type or mimetypes.guess_type(orig_name)[0] or "application/octet-stream").lower()
    should_optimize = optimize.lower() in {"1", "true", "yes", "on"} and content_type.startswith("image/")

    if should_optimize:
        payload, final_ext, final_mime = _maybe_optimize_image(raw, orig_ext, max_width=max_width, quality=quality)
    else:
        payload, final_ext, final_mime = raw, orig_ext, content_type

    size_bytes = len(payload)
    month_dir = datetime.now(timezone.utc).strftime("%Y-%m")
    rel_key = f"{bucket}/{month_dir}/{uuid.uuid4().hex}{final_ext}"
    dest = _resolve_key_path(rel_key)

    with _quota_lock:
        if _cached_used_bytes is None:
            _cached_used_bytes, _ = _scan_usage()
        if _cached_used_bytes + size_bytes > MAX_STORAGE_BYTES:
            raise HTTPException(
                status_code=507,
                detail="Storage quota of 50 GB reached. Delete older files before uploading more.",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        _cached_used_bytes += size_bytes
        used_now = _cached_used_bytes

    return JSONResponse(
        {
            "ok": True,
            "key": rel_key,
            "project": bucket,
            "url": f"{PUBLIC_BASE_URL}/files/{rel_key}",
            "sizeBytes": size_bytes,
            "mimeType": final_mime,
            "quota": _quota_summary(used_now),
        }
    )


@app.get("/files/{key:path}")
def serve_file(key: str):
    target = _resolve_key_path(key)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        path=target,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/api/files/{key:path}", dependencies=[Depends(_require_auth)])
def delete_file(key: str):
    global _cached_used_bytes
    target = _resolve_key_path(key)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    with _quota_lock:
        try:
            removed_size = target.stat().st_size
        except OSError:
            removed_size = 0
        target.unlink(missing_ok=True)
        # Clean up empty leaf month/project subdirectories (never DATA_DIR)
        parent = target.parent
        while parent != DATA_DIR and parent.is_relative_to(DATA_DIR):
            try:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
                parent = parent.parent
            except OSError:
                break
        if _cached_used_bytes is None:
            _cached_used_bytes, _ = _scan_usage()
        else:
            _cached_used_bytes = max(0, _cached_used_bytes - removed_size)
        used_now = _cached_used_bytes
    return {"ok": True, "deleted": key, "freedBytes": removed_size, "quota": _quota_summary(used_now)}


def _render_login_html() -> str:
    safe_base = html.escape(PUBLIC_BASE_URL)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Acceso · Media Storage · dario10.pw</title>
  <style>
    :root {{ color-scheme: light; font-family: 'Inter', system-ui, -apple-system, sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100dvh;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at top right, #efe4da 0%, #f6f2ed 55%, #f2ece5 100%);
      color: #2b211d;
      padding: 1.25rem;
    }}
    .card {{
      width: 100%;
      max-width: 410px;
      background: #fffdf9;
      border: 1px solid #e3d9cf;
      border-radius: 26px;
      padding: 2rem;
      box-shadow: 0 22px 55px rgba(43, 33, 29, 0.1);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.35rem 0.75rem;
      border-radius: 999px;
      background: #f2ddd6;
      color: #8e4637;
      font-size: 0.72rem;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 1rem;
    }}
    h1 {{ margin: 0 0 0.4rem; font-family: Georgia, serif; font-size: 1.85rem; letter-spacing: -0.02em; }}
    p.sub {{ margin: 0 0 1.5rem; color: #75655c; font-size: 0.9rem; line-height: 1.45; }}
    label {{ display: block; font-size: 0.82rem; font-weight: 700; color: #4b3b35; margin-bottom: 0.4rem; }}
    .field {{ margin-bottom: 1rem; position: relative; }}
    input {{
      width: 100%;
      font: inherit;
      padding: 0.78rem 0.95rem;
      border-radius: 14px;
      border: 1px solid #d7cbc0;
      background: #fff;
      color: #2b211d;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    input:focus {{ border-color: #b96550; box-shadow: 0 0 0 3px rgba(185, 101, 80, 0.15); }}
    .pw-wrap input {{ padding-right: 3rem; }}
    .pw-toggle {{
      position: absolute;
      right: 0.55rem;
      top: 50%;
      transform: translateY(-50%);
      border: 0;
      background: transparent;
      color: #7c6a62;
      cursor: pointer;
      width: 2.2rem;
      height: 2.2rem;
      display: grid;
      place-items: center;
      border-radius: 999px;
    }}
    .pw-toggle:hover {{ background: #f3ece6; }}
    button.primary {{
      width: 100%;
      border: 0;
      border-radius: 999px;
      padding: 0.88rem 1.25rem;
      background: #2b211d;
      color: #fff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      transition: background-color 0.15s, transform 0.12s;
      margin-top: 0.35rem;
    }}
    button.primary:hover {{ background: #b96550; }}
    button.primary:active {{ transform: scale(0.98); }}
    .err {{
      display: none;
      margin-bottom: 1rem;
      padding: 0.7rem 0.9rem;
      border-radius: 12px;
      background: #fbe9e5;
      border: 1px solid #ecc6bc;
      color: #9e3d2b;
      font-size: 0.84rem;
      font-weight: 600;
    }}
    .err.show {{ display: block; }}
    .foot {{ margin-top: 1.25rem; text-align: center; font-size: 0.76rem; color: #8c7a70; word-break: break-all; }}
  </style>
</head>
<body>
  <main class="card">
    <span class="badge">Storage · 50 GB</span>
    <h1>Gestor multimedia</h1>
    <p class="sub">Iniciá sesión para explorar carpetas, rutas, espacio ocupado y administrar los archivos.</p>
    <div id="login-err" class="err" role="alert"></div>
    <form id="login-form">
      <div class="field">
        <label for="username">Usuario</label>
        <input id="username" name="username" type="text" autocomplete="username" required autofocus placeholder="dario" />
      </div>
      <div class="field">
        <label for="password">Contraseña</label>
        <div class="pw-wrap" style="position:relative">
          <input id="password" name="password" type="password" autocomplete="current-password" required placeholder="••••••••" />
          <button type="button" class="pw-toggle" id="pw-toggle" aria-label="Mostrar contraseña">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"></path>
              <circle cx="12" cy="12" r="3"></circle>
            </svg>
          </button>
        </div>
      </div>
      <button type="submit" class="primary" id="login-submit">Entrar al gestor</button>
    </form>
    <p class="foot">{safe_base}</p>
  </main>
  <script>
    const pwInput = document.getElementById("password");
    document.getElementById("pw-toggle").addEventListener("click", () => {{
      pwInput.type = pwInput.type === "password" ? "text" : "password";
    }});
    document.getElementById("login-form").addEventListener("submit", async (e) => {{
      e.preventDefault();
      const errEl = document.getElementById("login-err");
      const btn = document.getElementById("login-submit");
      errEl.classList.remove("show");
      btn.disabled = true;
      btn.textContent = "Verificando...";
      try {{
        const res = await fetch("/auth/login", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            username: document.getElementById("username").value,
            password: pwInput.value
          }})
        }});
        const data = await res.json();
        if (res.ok && data.ok) {{
          location.reload();
        }} else {{
          errEl.textContent = data.detail || "Usuario o contraseña incorrectos.";
          errEl.classList.add("show");
        }}
      }} catch (err) {{
        errEl.textContent = "Error de conexión con el servidor.";
        errEl.classList.add("show");
      }} finally {{
        btn.disabled = false;
        btn.textContent = "Entrar al gestor";
      }}
    }});
  </script>
</body>
</html>"""


def _render_file_manager_html(username: str, q: dict) -> str:
    safe_base = html.escape(PUBLIC_BASE_URL)
    safe_user = html.escape(username)
    safe_dir = html.escape(str(DATA_DIR))
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Gestor de Archivos · Media Storage</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      --bg: #f6f2ed;
      --card: #fffdf9;
      --soft: #f3ece5;
      --border: #e3d9cf;
      --ink: #2b211d;
      --muted: #75655c;
      --accent: #b96550;
      --green-bg: #dce8df;
      --green-fg: #3b5d47;
      --danger-bg: #fbeae6;
      --danger-fg: #a13f2d;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(246, 242, 237, 0.92);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
    }}
    .topbar-inner {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 0.85rem 1rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      flex-wrap: wrap;
    }}
    .brand {{ display: flex; align-items: center; gap: 0.75rem; }}
    .brand-icon {{
      width: 38px;
      height: 38px;
      border-radius: 12px;
      background: var(--accent);
      color: #fff;
      display: grid;
      place-items: center;
      flex-shrink: 0;
    }}
    .brand h1 {{ margin: 0; font-family: Georgia, serif; font-size: 1.25rem; line-height: 1.1; }}
    .brand span {{ font-size: 0.75rem; color: var(--muted); }}
    .top-actions {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}
    .user-chip {{
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.42rem 0.8rem;
      border-radius: 999px;
      background: var(--card);
      border: 1px solid var(--border);
      font-size: 0.8rem;
      font-weight: 700;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      border: 0;
      border-radius: 999px;
      padding: 0.55rem 1rem;
      font: inherit;
      font-size: 0.82rem;
      font-weight: 700;
      cursor: pointer;
      transition: background-color 0.15s, transform 0.12s;
    }}
    .btn:active {{ transform: scale(0.97); }}
    .btn-dark {{ background: var(--ink); color: #fff; }}
    .btn-dark:hover {{ background: var(--accent); }}
    .btn-soft {{ background: var(--card); color: var(--ink); border: 1px solid var(--border); }}
    .btn-soft:hover {{ background: var(--soft); }}
    .btn-danger {{ background: var(--danger-bg); color: var(--danger-fg); border: 1px solid #ecc6bc; }}
    .btn-danger:hover {{ background: #f5dad3; }}
    .btn-sm {{ padding: 0.38rem 0.65rem; font-size: 0.74rem; border-radius: 10px; }}

    .wrap {{ max-width: 1180px; margin: 0 auto; padding: 1.25rem 1rem 3rem; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 22px;
      padding: 1.25rem;
      box-shadow: 0 10px 28px rgba(43, 33, 29, 0.04);
      margin-bottom: 1.15rem;
    }}
    .quota-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
    .quota-head h2 {{ margin: 0; font-family: Georgia, serif; font-size: 1.35rem; }}
    .bar-bg {{ height: 12px; border-radius: 999px; background: #ebe3da; overflow: hidden; margin: 0.75rem 0; }}
    .bar-fill {{ height: 100%; background: var(--accent); width: {min(100, q['usedPercent'])}%; transition: width 0.3s ease; }}
    .quota-meta {{ display: flex; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap; font-size: 0.82rem; color: var(--muted); }}
    code.path {{
      background: var(--soft);
      padding: 0.15rem 0.45rem;
      border-radius: 7px;
      font-size: 0.76rem;
      color: #554239;
    }}

    .layout {{ display: grid; grid-template-columns: 260px 1fr; gap: 1.15rem; align-items: start; }}
    @media (max-width: 860px) {{ .layout {{ grid-template-columns: 1fr; }} }}

    .sidebar h3 {{ margin: 0 0 0.75rem; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.14em; color: var(--muted); }}
    .folder-list {{ display: flex; flex-direction: column; gap: 0.35rem; }}
    .folder-btn {{
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.5rem;
      text-align: left;
      padding: 0.55rem 0.75rem;
      border-radius: 12px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--ink);
      font: inherit;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .folder-btn:hover {{ background: var(--soft); }}
    .folder-btn.active {{ background: var(--ink); color: #fff; }}
    .folder-btn.sub {{ padding-left: 1.5rem; font-size: 0.78rem; font-weight: 500; }}
    .count-pill {{
      font-size: 0.7rem;
      padding: 0.12rem 0.48rem;
      border-radius: 999px;
      background: rgba(117, 101, 92, 0.14);
      font-weight: 700;
    }}
    .folder-btn.active .count-pill {{ background: rgba(255, 255, 255, 0.22); }}

    .toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }}
    .search-box {{ flex: 1; min-width: 200px; position: relative; }}
    .search-box input {{
      width: 100%;
      padding: 0.6rem 0.9rem 0.6rem 2.3rem;
      border-radius: 999px;
      border: 1px solid #d7cbc0;
      background: #fff;
      font: inherit;
      font-size: 0.84rem;
      outline: none;
    }}
    .search-box input:focus {{ border-color: var(--accent); }}
    .search-box svg {{ position: absolute; left: 0.8rem; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }}

    .view-toggle {{ display: inline-flex; background: var(--soft); padding: 0.22rem; border-radius: 999px; gap: 0.15rem; }}
    .view-toggle button {{
      border: 0;
      background: transparent;
      padding: 0.35rem 0.7rem;
      border-radius: 999px;
      font: inherit;
      font-size: 0.76rem;
      font-weight: 700;
      color: var(--muted);
      cursor: pointer;
    }}
    .view-toggle button.active {{ background: #fff; color: var(--ink); box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08); }}

    .breadcrumb {{ display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; font-size: 0.82rem; color: var(--muted); margin-bottom: 0.9rem; }}
    .breadcrumb strong {{ color: var(--ink); }}

    .files-grid {{ display: grid; gap: 0.85rem; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }}
    .file-card {{
      border: 1px solid var(--border);
      border-radius: 18px;
      background: #fff;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      transition: transform 0.15s, box-shadow 0.15s;
    }}
    .file-card:hover {{ transform: translateY(-2px); box-shadow: 0 10px 24px rgba(43, 33, 29, 0.08); }}
    .thumb-wrap {{
      position: relative;
      aspect-ratio: 4 / 3;
      background: #f0e9e2;
      overflow: hidden;
      cursor: pointer;
      display: grid;
      place-items: center;
    }}
    .thumb-wrap img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .proj-tag {{
      position: absolute;
      top: 0.5rem;
      left: 0.5rem;
      padding: 0.18rem 0.55rem;
      border-radius: 999px;
      background: rgba(43, 33, 29, 0.75);
      backdrop-filter: blur(6px);
      color: #fff;
      font-size: 0.68rem;
      font-weight: 700;
    }}
    .file-body {{ padding: 0.75rem 0.85rem; flex: 1; display: flex; flex-direction: column; gap: 0.35rem; }}
    .file-name {{ font-weight: 700; font-size: 0.82rem; word-break: break-all; line-height: 1.25; }}
    .file-path {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.7rem; color: var(--muted); word-break: break-all; }}
    .file-meta {{ font-size: 0.72rem; color: var(--muted); display: flex; justify-content: space-between; margin-top: auto; padding-top: 0.25rem; }}
    .file-actions {{ display: flex; gap: 0.35rem; padding: 0 0.85rem 0.8rem; flex-wrap: wrap; }}

    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
    th, td {{ text-align: left; padding: 0.65rem 0.6rem; border-bottom: 1px solid #eee5dc; vertical-align: middle; }}
    th {{ font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
    .mini-thumb {{ width: 42px; height: 42px; border-radius: 10px; object-fit: cover; background: #f0e9e2; display: block; cursor: pointer; }}

    .empty {{
      border: 1px dashed #d4c6b9;
      border-radius: 18px;
      padding: 2.5rem 1rem;
      text-align: center;
      color: var(--muted);
    }}

    dialog {{
      border: 0;
      border-radius: 24px;
      padding: 0;
      width: min(94vw, 500px);
      background: var(--card);
      color: var(--ink);
      box-shadow: 0 25px 70px rgba(30, 20, 16, 0.32);
    }}
    dialog::backdrop {{ background: rgba(34, 24, 20, 0.5); backdrop-filter: blur(6px); }}
    .dlg-inner {{ padding: 1.5rem; }}
    .dlg-head {{ display: flex; justify-content: space-between; align-items: start; gap: 1rem; margin-bottom: 1rem; }}
    .dlg-head h3 {{ margin: 0; font-family: Georgia, serif; font-size: 1.35rem; }}
    .dlg-close {{
      width: 36px; height: 36px; border-radius: 999px; border: 0; background: var(--soft);
      color: var(--ink); cursor: pointer; font-weight: 700; display: grid; place-items: center;
    }}
    .toast {{
      position: fixed; bottom: 1.25rem; right: 1.25rem; z-index: 50;
      background: var(--ink); color: #fff; padding: 0.7rem 1.05rem; border-radius: 14px;
      font-size: 0.84rem; font-weight: 600; box-shadow: 0 12px 30px rgba(0, 0, 0, 0.22);
      display: none;
    }}
    .toast.show {{ display: block; }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <div class="brand-icon" aria-hidden="true">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"></path>
          </svg>
        </div>
        <div>
          <h1>Gestor de Archivos</h1>
          <span>{safe_base} · montado en {safe_dir}</span>
        </div>
      </div>
      <div class="top-actions">
        <span class="user-chip">@{safe_user}</span>
        <button type="button" class="btn btn-dark" onclick="openUploadModal()">+ Subir archivos</button>
        <button type="button" class="btn btn-soft" onclick="refreshAll()">Actualizar</button>
        <button type="button" class="btn btn-danger" onclick="logout()">Salir</button>
      </div>
    </div>
  </header>

  <main class="wrap">
    <section class="card">
      <div class="quota-head">
        <h2>Almacenamiento compartido · 50 GB</h2>
        <strong id="quota-text">{q['usedGigabytes']} GB usados de {q['maxGigabytes']} GB ({q['usedPercent']}%)</strong>
      </div>
      <div class="bar-bg"><div class="bar-fill" id="bar-fill"></div></div>
      <div class="quota-meta">
        <span id="quota-sub">Calculando archivos y espacio disponible...</span>
        <span>Ruta en servidor: <code class="path">{safe_dir}</code></span>
      </div>
    </section>

    <div class="layout">
      <aside class="card sidebar">
        <h3>Carpetas y proyectos</h3>
        <div id="folders-nav" class="folder-list"></div>
      </aside>

      <section class="card">
        <div class="toolbar">
          <div class="search-box">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.3-4.3"></path>
            </svg>
            <input id="search-input" type="search" placeholder="Buscar por nombre, carpeta o ruta..." />
          </div>
          <div class="view-toggle" role="group" aria-label="Modo de vista">
            <button type="button" id="btn-view-grid" class="active" onclick="setView('grid')">Grilla</button>
            <button type="button" id="btn-view-list" onclick="setView('list')">Lista</button>
          </div>
        </div>

        <div id="breadcrumb" class="breadcrumb"></div>
        <div id="files-container"></div>
      </section>
    </div>
  </main>

  <dialog id="dlg-upload">
    <div class="dlg-inner">
      <div class="dlg-head">
        <h3>Subir archivos</h3>
        <button type="button" class="dlg-close" onclick="document.getElementById('dlg-upload').close()" aria-label="Cerrar">✕</button>
      </div>
      <form id="upload-form" style="display:grid;gap:0.9rem">
        <div>
          <label style="display:block;font-size:.82rem;font-weight:700;margin-bottom:.35rem">Carpeta / Proyecto</label>
          <input id="up-project" type="text" value="nosotros" required style="width:100%;padding:.65rem .85rem;border:1px solid #d7cbc0;border-radius:12px;font:inherit" placeholder="Ej. nosotros, general, backup" />
        </div>
        <div>
          <label style="display:block;font-size:.82rem;font-weight:700;margin-bottom:.35rem">Seleccionar archivos</label>
          <input id="up-files" type="file" multiple required style="width:100%;padding:.5rem;border:1px dashed #c8b8aa;border-radius:12px;background:#faf6f1" />
        </div>
        <label style="display:flex;align-items:center;gap:.5rem;font-size:.82rem;font-weight:600">
          <input id="up-opt" type="checkbox" checked />
          Optimizar imágenes a WebP (máx. 1800 px)
        </label>
        <p id="up-status" style="margin:0;font-size:.82rem;color:var(--muted)"></p>
        <button type="submit" class="btn btn-dark" id="up-submit" style="justify-content:center;padding:.8rem">Subir al almacenamiento</button>
      </form>
    </div>
  </dialog>

  <dialog id="dlg-delete">
    <div class="dlg-inner">
      <div class="dlg-head">
        <h3>Eliminar archivo</h3>
        <button type="button" class="dlg-close" onclick="document.getElementById('dlg-delete').close()" aria-label="Cerrar">✕</button>
      </div>
      <div id="del-preview" style="margin-bottom:0.9rem"></div>
      <p style="margin:0 0 0.5rem;font-size:.9rem">¿Seguro que querés borrar permanentemente este archivo del almacenamiento?</p>
      <p style="margin:0 0 1.2rem"><code id="del-path" class="path"></code></p>
      <div style="display:flex;gap:.5rem;justify-content:flex-end">
        <button type="button" class="btn btn-soft" onclick="document.getElementById('dlg-delete').close()">Cancelar</button>
        <button type="button" class="btn btn-danger" id="del-confirm-btn">Sí, eliminar archivo</button>
      </div>
    </div>
  </dialog>

  <dialog id="dlg-preview" style="width:min(95vw,760px)">
    <div class="dlg-inner">
      <div class="dlg-head">
        <h3 id="prev-name" style="font-size:1.05rem;word-break:break-all">Vista previa</h3>
        <button type="button" class="dlg-close" onclick="document.getElementById('dlg-preview').close()" aria-label="Cerrar">✕</button>
      </div>
      <div id="prev-body" style="background:#1c1512;border-radius:16px;overflow:hidden;display:grid;place-items:center;max-height:70dvh"></div>
      <div style="margin-top:.85rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap;font-size:.8rem;color:var(--muted)">
        <code id="prev-path" class="path"></code>
        <a id="prev-open" href="#" target="_blank" rel="noopener" class="btn btn-soft btn-sm" style="text-decoration:none">Abrir original ↗</a>
      </div>
    </div>
  </dialog>

  <div id="toast" class="toast" role="status"></div>

  <script>
    const state = {{
      stats: null,
      files: [],
      folders: [],
      activePrefix: "",
      search: "",
      view: "grid",
      deleteKey: null,
    }};

    function esc(s) {{
      return String(s ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function fmtSize(bytes) {{
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + " MB";
      return (bytes / (1024 * 1024 * 1024)).toFixed(3) + " GB";
    }}

    function fmtDate(iso) {{
      try {{
        return new Intl.DateTimeFormat("es-AR", {{
          day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"
        }}).format(new Date(iso));
      }} catch {{
        return iso;
      }}
    }}

    function isImg(it) {{
      return (it.mimeType || "").startsWith("image/") || /\\.(webp|jpe?g|png|gif|avif)$/i.test(it.key);
    }}

    function toast(msg) {{
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      clearTimeout(toast._t);
      toast._t = setTimeout(() => el.classList.remove("show"), 2600);
    }}

    async function refreshAll() {{
      const params = new URLSearchParams({{ limit: "500" }});
      if (state.activePrefix) params.set("prefix", state.activePrefix);
      if (state.search) params.set("search", state.search);
      const [rStats, rFiles] = await Promise.all([
        fetch("/api/stats"),
        fetch("/api/files?" + params.toString())
      ]);
      if (rStats.status === 401 || rFiles.status === 401) {{
        location.reload();
        return;
      }}
      state.stats = await rStats.json();
      const fd = await rFiles.json();
      state.files = fd.items || [];
      if (!state.activePrefix && !state.search) {{
        state.folders = fd.folders || [];
      }}
      render();
    }}

    function setPrefix(prefix) {{
      state.activePrefix = prefix;
      refreshAll();
    }}

    function setView(v) {{
      state.view = v;
      document.getElementById("btn-view-grid").classList.toggle("active", v === "grid");
      document.getElementById("btn-view-list").classList.toggle("active", v === "list");
      renderFiles();
    }}

    function render() {{
      const q = state.stats?.quota;
      if (q) {{
        document.getElementById("bar-fill").style.width = Math.min(100, Math.max(q.usedBytes > 0 ? 1 : 0, q.usedPercent)) + "%";
        document.getElementById("quota-text").textContent = `${{fmtSize(q.usedBytes)}} usados de ${{q.maxGigabytes}} GB (${{q.usedPercent}}%)`;
        const totalFiles = (state.stats.buckets || []).reduce((acc, b) => acc + b.files, 0);
        document.getElementById("quota-sub").textContent = `${{totalFiles}} archivo${{totalFiles === 1 ? "" : "s"}} totales · ${{fmtSize(q.freeBytes)}} libres`;
      }}

      // Sidebar folder tree
      const nav = document.getElementById("folders-nav");
      const buckets = state.stats?.buckets || [];
      const totalFilesAll = buckets.reduce((a, b) => a + b.files, 0);
      let html = `<button type="button" class="folder-btn ${{state.activePrefix === "" ? "active" : ""}}" onclick="setPrefix('')">
        <span>📁 Todos los archivos</span>
        <span class="count-pill">${{totalFilesAll}}</span>
      </button>`;

      buckets.forEach((b) => {{
        const pActive = state.activePrefix === b.project;
        html += `<button type="button" class="folder-btn ${{pActive ? "active" : ""}}" onclick="setPrefix('${{esc(b.project)}}')">
          <span>📂 ${{esc(b.project)}} <small style="opacity:.7">(${{b.usedMegabytes}} MB)</small></span>
          <span class="count-pill">${{b.files}}</span>
        </button>`;
        state.folders
          .filter((f) => f.path.startsWith(b.project + "/"))
          .forEach((f) => {{
            const subLabel = f.path.slice(b.project.length + 1);
            const sActive = state.activePrefix === f.path;
            html += `<button type="button" class="folder-btn sub ${{sActive ? "active" : ""}}" onclick="setPrefix('${{esc(f.path)}}')">
              <span>↳ ${{esc(subLabel)}}</span>
              <span class="count-pill">${{f.files}}</span>
            </button>`;
          }});
      }});
      nav.innerHTML = html;

      // Breadcrumb
      const bc = document.getElementById("breadcrumb");
      if (!state.activePrefix) {{
        bc.innerHTML = `Ubicación actual: <strong>/data</strong> (${{state.files.length}} elementos)`;
      }} else {{
        bc.innerHTML = `Ubicación actual: <a href="#" onclick="setPrefix('');return false;" style="color:var(--accent);font-weight:700;text-decoration:none">/data</a> / <strong>${{esc(state.activePrefix)}}</strong> (${{state.files.length}} elementos)`;
      }}

      renderFiles();
    }}

    function renderFiles() {{
      const box = document.getElementById("files-container");
      if (!state.files.length) {{
        box.innerHTML = `<div class="empty"><strong>No hay archivos acá</strong><p style="margin:.4rem 0 0;font-size:.86rem">Cuando subas fotos desde la app o desde "+ Subir archivos" vas a verlas con su carpeta y ruta exacta.</p></div>`;
        return;
      }}

      if (state.view === "grid") {{
        box.innerHTML = `<div class="files-grid">${{state.files.map((it) => `
          <article class="file-card">
            <div class="thumb-wrap" onclick="openPreview('${{esc(it.key)}}')">
              <span class="proj-tag">${{esc(it.project)}}</span>
              ${{isImg(it)
                ? `<img src="${{esc(it.url)}}" loading="lazy" alt="${{esc(it.name)}}" />`
                : `<span style="font-size:1.7rem">📄</span>`}}
            </div>
            <div class="file-body">
              <div class="file-name">${{esc(it.name)}}</div>
              <div class="file-path">${{esc(it.diskPath)}}</div>
              <div class="file-meta">
                <span>${{fmtSize(it.sizeBytes)}}</span>
                <span>${{fmtDate(it.modifiedAt)}}</span>
              </div>
            </div>
            <div class="file-actions">
              <button type="button" class="btn btn-soft btn-sm" onclick="copyUrl('${{esc(it.url)}}')">Copiar URL</button>
              <a href="${{esc(it.url)}}" target="_blank" rel="noopener" class="btn btn-soft btn-sm" style="text-decoration:none">Abrir</a>
              <button type="button" class="btn btn-danger btn-sm" onclick="askDelete('${{esc(it.key)}}')">Borrar</button>
            </div>
          </article>`).join("")}}</div>`;
      }} else {{
        box.innerHTML = `<div class="table-wrap"><table>
          <thead>
            <tr>
              <th>Vista</th>
              <th>Archivo y ubicación en disco</th>
              <th>Proyecto</th>
              <th>Tamaño</th>
              <th>Fecha</th>
              <th>Acciones</th>
            </tr>
          </thead>
          <tbody>${{state.files.map((it) => `
            <tr>
              <td>${{isImg(it)
                ? `<img src="${{esc(it.url)}}" class="mini-thumb" loading="lazy" onclick="openPreview('${{esc(it.key)}}')" alt="" />`
                : `<div class="mini-thumb" style="display:grid;place-items:center">📄</div>`}}</td>
              <td>
                <div style="font-weight:700;word-break:break-all">${{esc(it.name)}}</div>
                <div class="file-path">${{esc(it.diskPath)}}</div>
              </td>
              <td><code class="path">${{esc(it.project)}}</code></td>
              <td style="white-space:nowrap">${{fmtSize(it.sizeBytes)}}</td>
              <td style="white-space:nowrap;color:var(--muted);font-size:.76rem">${{fmtDate(it.modifiedAt)}}</td>
              <td style="white-space:nowrap">
                <button type="button" class="btn btn-soft btn-sm" onclick="copyUrl('${{esc(it.url)}}')">Copiar URL</button>
                <button type="button" class="btn btn-danger btn-sm" onclick="askDelete('${{esc(it.key)}}')">Borrar</button>
              </td>
            </tr>`).join("")}}</tbody>
        </table></div>`;
      }}
    }}

    function findItem(key) {{
      return state.files.find((f) => f.key === key);
    }}

    async function copyUrl(url) {{
      try {{
        await navigator.clipboard.writeText(url);
        toast("URL copiada al portapapeles");
      }} catch {{
        prompt("Copiá la URL:", url);
      }}
    }}

    function openPreview(key) {{
      const it = findItem(key);
      if (!it) return;
      document.getElementById("prev-name").textContent = it.name;
      document.getElementById("prev-path").textContent = it.diskPath;
      document.getElementById("prev-open").href = it.url;
      document.getElementById("prev-body").innerHTML = isImg(it)
        ? `<img src="${{esc(it.url)}}" style="max-width:100%;max-height:68dvh;object-fit:contain;display:block" alt="" />`
        : `<p style="color:#fff;padding:2rem">Sin vista previa disponible para ${{esc(it.mimeType)}}</p>`;
      document.getElementById("dlg-preview").showModal();
    }}

    function askDelete(key) {{
      const it = findItem(key);
      if (!it) return;
      state.deleteKey = key;
      document.getElementById("del-path").textContent = it.diskPath + " (" + fmtSize(it.sizeBytes) + ")";
      document.getElementById("del-preview").innerHTML = isImg(it)
        ? `<img src="${{esc(it.url)}}" style="width:100%;max-height:190px;object-fit:cover;border-radius:14px;display:block" alt="" />`
        : "";
      document.getElementById("dlg-delete").showModal();
    }}

    document.getElementById("del-confirm-btn").addEventListener("click", async () => {{
      const key = state.deleteKey;
      if (!key) return;
      const btn = document.getElementById("del-confirm-btn");
      btn.disabled = true;
      btn.textContent = "Eliminando...";
      try {{
        const res = await fetch("/api/files/" + key.split("/").map(encodeURIComponent).join("/"), {{
          method: "DELETE"
        }});
        if (res.ok) {{
          document.getElementById("dlg-delete").close();
          toast("Archivo eliminado del servidor");
          await refreshAll();
        }} else {{
          toast("No se pudo eliminar (" + res.status + ")");
        }}
      }} finally {{
        btn.disabled = false;
        btn.textContent = "Sí, eliminar archivo";
      }}
    }});

    function openUploadModal() {{
      if (state.activePrefix) {{
        document.getElementById("up-project").value = state.activePrefix.split("/")[0];
      }}
      document.getElementById("up-status").textContent = "";
      document.getElementById("dlg-upload").showModal();
    }}

    document.getElementById("upload-form").addEventListener("submit", async (e) => {{
      e.preventDefault();
      const files = document.getElementById("up-files").files;
      const proj = document.getElementById("up-project").value.trim() || "general";
      const opt = document.getElementById("up-opt").checked ? "1" : "0";
      const st = document.getElementById("up-status");
      const btn = document.getElementById("up-submit");
      if (!files.length) return;
      btn.disabled = true;
      try {{
        for (const f of files) {{
          st.textContent = `Subiendo ${{f.name}}...`;
          const fd = new FormData();
          fd.append("project", proj);
          fd.append("optimize", opt);
          fd.append("file", f);
          const res = await fetch("/api/upload", {{ method: "POST", body: fd }});
          if (!res.ok) {{
            st.textContent = `Error al subir ${{f.name}} (${{res.status}})`;
            return;
          }}
        }}
        document.getElementById("up-files").value = "";
        document.getElementById("dlg-upload").close();
        toast("Archivos subidos correctamente");
        await refreshAll();
      }} finally {{
        btn.disabled = false;
      }}
    }});

    document.getElementById("search-input").addEventListener("input", (e) => {{
      clearTimeout(window._st);
      state.search = e.target.value.trim();
      window._st = setTimeout(refreshAll, 180);
    }});

    async function logout() {{
      await fetch("/auth/logout", {{ method: "POST" }});
      location.reload();
    }}

    refreshAll();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = _get_session_user(request)
    if not user:
        return HTMLResponse(_render_login_html())
    used = _get_used_bytes()
    q = _quota_summary(used)
    return HTMLResponse(_render_file_manager_html(user, q))
