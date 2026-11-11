import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from django.conf import settings


def is_remote_storage_configured() -> bool:
    return bool(getattr(settings, "MEDIA_STORAGE_URL", "") and getattr(settings, "MEDIA_STORAGE_KEY", ""))


def upload_to_media_storage(uploaded_file, bucket: str | None = None) -> dict | None:
    """
    Upload an image/file to the dedicated Coolify Media Storage server (`storage.dario10.pw`).
    Returns `{"key": ..., "url": ..., "sizeBytes": ...}` or `None` if unavailable/unconfigured
    so callers can cleanly fall back to local Django ImageField storage.
    """
    base_url = getattr(settings, "MEDIA_STORAGE_URL", "").rstrip("/")
    api_key = getattr(settings, "MEDIA_STORAGE_KEY", "")
    target_bucket = bucket or getattr(settings, "MEDIA_STORAGE_BUCKET", "nosotros")
    if not base_url or not api_key:
        return None

    uploaded_file.seek(0)
    raw = uploaded_file.read()
    uploaded_file.seek(0)
    if not raw:
        return None

    filename = getattr(uploaded_file, "name", None) or "photo.webp"
    content_type = (
        getattr(uploaded_file, "content_type", None)
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    boundary = f"----NosotrosBoundary{uuid.uuid4().hex}"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))

    add_field("project", target_bucket)
    add_field("optimize", "1")
    add_field("max_width", "1600")
    add_field("quality", "82")

    safe_name = filename.replace('"', "")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(raw)
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        url=f"{base_url}/api/upload",
        data=bytes(body),
        method="POST",
        headers={
            "X-API-Key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "NosotrosAgenda/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            resp_ct = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" not in resp_ct:
                return None
            payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("ok") and payload.get("url") and payload.get("key"):
                return payload
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None
    return None


def delete_from_media_storage(remote_key: str) -> bool:
    """Best-effort delete of a remote object key on Media Storage."""
    base_url = getattr(settings, "MEDIA_STORAGE_URL", "").rstrip("/")
    api_key = getattr(settings, "MEDIA_STORAGE_KEY", "")
    if not base_url or not api_key or not remote_key:
        return False

    quoted = urllib.parse.quote(remote_key.lstrip("/"), safe="/")
    req = urllib.request.Request(
        url=f"{base_url}/api/files/{quoted}",
        method="DELETE",
        headers={
            "X-API-Key": api_key,
            "User-Agent": "NosotrosAgenda/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
