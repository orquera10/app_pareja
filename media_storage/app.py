import hmac
import io
import mimetypes
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
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

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
SAFE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")

_quota_lock = threading.Lock()
_cached_used_bytes: int | None = None

app = FastAPI(title="Dario Media Storage", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


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


def _require_api_key(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    api_key: str | None = Query(default=None),
) -> None:
    if not STORAGE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="STORAGE_API_KEY is not configured on the media server.",
        )
    candidate = (x_api_key or "").strip()
    if not candidate and authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    if not candidate and api_key:
        candidate = api_key.strip()
    if not candidate or not hmac.compare_digest(candidate, STORAGE_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


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


@app.get("/healthz")
def healthz():
    used = _get_used_bytes()
    return {
        "ok": True,
        "service": "dario-media-storage",
        "quota": _quota_summary(used),
    }


@app.get("/api/stats", dependencies=[Depends(_require_api_key)])
def api_stats():
    global _cached_used_bytes
    total, buckets = _scan_usage()
    with _quota_lock:
        _cached_used_bytes = total
    return {
        "ok": True,
        "service": "dario-media-storage",
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


@app.get("/api/files", dependencies=[Depends(_require_api_key)])
def api_list_files(project: str = "", limit: int = 80):
    target_bucket = _sanitize_project(project) if project else ""
    items = []
    search_root = (DATA_DIR / target_bucket) if target_bucket else DATA_DIR
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
                items.append(
                    {
                        "key": rel_key,
                        "url": f"{PUBLIC_BASE_URL}/files/{rel_key}",
                        "sizeBytes": st.st_size,
                        "modifiedAt": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
    items.sort(key=lambda x: x["modifiedAt"], reverse=True)
    return {
        "ok": True,
        "items": items[: max(1, min(limit, 500))],
        "totalFound": len(items),
        "quota": _quota_summary(),
    }


@app.post("/api/upload", dependencies=[Depends(_require_api_key)])
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


@app.delete("/api/files/{key:path}", dependencies=[Depends(_require_api_key)])
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
        if _cached_used_bytes is None:
            _cached_used_bytes, _ = _scan_usage()
        else:
            _cached_used_bytes = max(0, _cached_used_bytes - removed_size)
        used_now = _cached_used_bytes
    return {"ok": True, "deleted": key, "freedBytes": removed_size, "quota": _quota_summary(used_now)}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    used = _get_used_bytes()
    q = _quota_summary(used)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Media Storage · dario10.pw</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; background: #f5f2ed; color: #2b211d; padding: 2rem 1rem; }}
    .wrap {{ max-width: 760px; margin: 0 auto; }}
    .card {{ background: #fffdf9; border: 1px solid #e3d9cf; border-radius: 20px; padding: 1.5rem; margin-bottom: 1.25rem; box-shadow: 0 10px 30px rgba(43,33,29,.05); }}
    h1 {{ margin: 0 0 .35rem; font-size: 1.65rem; }}
    .muted {{ color: #75655c; font-size: .92rem; }}
    .bar-bg {{ height: 14px; border-radius: 999px; background: #ebe3da; overflow: hidden; margin: .8rem 0; }}
    .bar-fill {{ height: 100%; background: #b96550; width: {min(100, q['usedPercent'])}%; transition: width .3s; }}
    input, button {{ font: inherit; border-radius: 12px; padding: .65rem .9rem; }}
    input {{ border: 1px solid #d7cbc0; background: #fff; width: 100%; box-sizing: border-box; }}
    button {{ border: 0; background: #2b211d; color: #fff; font-weight: 600; cursor: pointer; }}
    button:hover {{ background: #b96550; }}
    pre {{ background: #241b18; color: #f6efe8; padding: 1rem; border-radius: 14px; overflow-x: auto; font-size: .82rem; }}
    .grid {{ display: grid; gap: .75rem; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); margin-top: 1rem; }}
    .item {{ border: 1px solid #e6ddd4; border-radius: 14px; padding: .5rem; background: #faf6f1; font-size: .78rem; }}
    .item img {{ width: 100%; height: 110px; object-fit: cover; border-radius: 10px; display: block; margin-bottom: .4rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Storage Multimedia · 50 GB</h1>
      <p class="muted">Servicio compartido multi-proyecto en <code>{PUBLIC_BASE_URL}</code></p>
      <div class="bar-bg"><div class="bar-fill" id="bar-fill"></div></div>
      <strong id="quota-text">{q['usedGigabytes']} GB usados de {q['maxGigabytes']} GB ({q['usedPercent']}%)</strong>
    </div>

    <div class="card">
      <h2 style="margin-top:0">Subir o explorar proyectos</h2>
      <div style="display:grid;gap:.75rem">
        <input id="api-key" type="password" placeholder="X-API-Key del almacenamiento" />
        <div style="display:grid;gap:.75rem;grid-template-columns:1fr auto">
          <input id="project-slug" type="text" value="general" placeholder="Proyecto (ej. nosotros, appgastos, alda)" />
          <button type="button" onclick="loadStats()">Ver archivos</button>
        </div>
        <div style="display:grid;gap:.75rem;grid-template-columns:1fr auto">
          <input id="file-input" type="file" multiple />
          <button type="button" onclick="uploadFiles()">Subir</button>
        </div>
        <p id="status" class="muted" style="margin:0"></p>
      </div>
      <div id="buckets" style="margin-top:1rem;font-size:.88rem"></div>
      <div id="files-grid" class="grid"></div>
    </div>

    <div class="card">
      <h3 style="margin-top:0">Uso rápido desde otros proyectos</h3>
      <pre>curl -X POST "{PUBLIC_BASE_URL}/api/upload" \\
  -H "X-API-Key: TU_API_KEY" \\
  -F "project=mi-otro-proyecto" \\
  -F "file=@foto.jpg"</pre>
    </div>
  </div>
  <script>
    const keyEl = document.getElementById("api-key");
    keyEl.value = sessionStorage.getItem("storage_api_key") || "";
    keyEl.addEventListener("input", () => sessionStorage.setItem("storage_api_key", keyEl.value));

    async function loadStats() {{
      const k = keyEl.value.trim();
      const proj = document.getElementById("project-slug").value.trim();
      const st = document.getElementById("status");
      if (!k) {{ st.textContent = "Ingresá tu X-API-Key."; return; }}
      st.textContent = "Cargando...";
      const [rStats, rFiles] = await Promise.all([
        fetch("/api/stats", {{ headers: {{ "X-API-Key": k }} }}),
        fetch("/api/files?project=" + encodeURIComponent(proj), {{ headers: {{ "X-API-Key": k }} }})
      ]);
      if (!rStats.ok) {{ st.textContent = "API Key inválida o error " + rStats.status; return; }}
      const stats = await rStats.json();
      const files = await rFiles.json();
      document.getElementById("bar-fill").style.width = Math.min(100, stats.quota.usedPercent) + "%";
      document.getElementById("quota-text").textContent = `${{stats.quota.usedGigabytes}} GB usados de ${{stats.quota.maxGigabytes}} GB (${{stats.quota.usedPercent}}%)`;
      document.getElementById("buckets").innerHTML = "<strong>Proyectos:</strong> " +
        (stats.buckets.length ? stats.buckets.map(b => `${{b.project}} (${{b.files}} arch · ${{b.usedMegabytes}} MB)`).join(" · ") : "Sin archivos aún");
      const grid = document.getElementById("files-grid");
      grid.innerHTML = (files.items || []).map(it => `
        <div class="item">
          ${{/\\.(webp|jpg|jpeg|png|gif)$/i.test(it.key) ? `<img src="${{it.url}}" loading="lazy" alt="">` : ""}}
          <div style="word-break:break-all;margin-bottom:.35rem">${{it.key}}</div>
          <a href="${{it.url}}" target="_blank" rel="noopener">Abrir</a>
        </div>`).join("");
      st.textContent = `Mostrando ${{files.items.length}} de ${{files.totalFound}} archivos.`;
    }}

    async function uploadFiles() {{
      const k = keyEl.value.trim();
      const proj = document.getElementById("project-slug").value.trim() || "general";
      const files = document.getElementById("file-input").files;
      const st = document.getElementById("status");
      if (!k || !files.length) {{ st.textContent = "Elegí archivos y pegá tu API Key."; return; }}
      for (const f of files) {{
        const fd = new FormData();
        fd.append("project", proj);
        fd.append("file", f);
        st.textContent = "Subiendo " + f.name + "...";
        const r = await fetch("/api/upload", {{ method: "POST", headers: {{ "X-API-Key": k }}, body: fd }});
        if (!r.ok) {{ st.textContent = "Error al subir " + f.name + " (" + r.status + ")"; return; }}
      }}
      document.getElementById("file-input").value = "";
      await loadStats();
    }}
  </script>
</body>
</html>"""
