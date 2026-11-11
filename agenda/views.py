import json
from datetime import datetime
from django.conf import settings
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import Memory, Plan


def _serialize_state():
    plans = [p.to_dict() for p in Plan.objects.all().order_by("date")]
    memories = [
        m.to_dict() for m in Memory.objects.all().order_by("-happened_at", "-created_at")
    ]
    return {"plans": plans, "memories": memories}


def _parse_iso_dt(value: str):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


@ensure_csrf_cookie
@require_GET
def index(request):
    state = _serialize_state()
    return render(
        request,
        "agenda/index.html",
        {
            "initial_state_json": json.dumps(state, ensure_ascii=False),
            "apk_release_url": "https://github.com/orquera10/app_pareja/releases/latest/download/Nosotros-v1.0.0.apk",
        },
    )


@require_GET
def api_state(request):
    return JsonResponse({"success": True, **_serialize_state()})


@require_POST
def api_create_plan(request):
    title = (request.POST.get("title") or "").strip()
    date_str = (request.POST.get("date") or "").strip()
    category = (request.POST.get("category") or "date").strip()
    location = (request.POST.get("location") or "").strip()
    description = (request.POST.get("description") or "").strip()

    field_errors = {}
    if not title:
        field_errors["title"] = "Escribí un nombre para el plan."
    elif len(title) > 80:
        field_errors["title"] = "El nombre no puede superar los 80 caracteres."

    dt = _parse_iso_dt(date_str)
    if not dt:
        field_errors["date"] = "Elegí una fecha y hora válidas."

    valid_categories = {c[0] for c in Plan.CATEGORY_CHOICES}
    if category not in valid_categories:
        field_errors["category"] = "Elegí un tipo de plan válido."

    if len(location) > 120:
        field_errors["location"] = "El lugar no puede superar los 120 caracteres."
    if len(description) > 500:
        field_errors["description"] = "Los detalles no pueden superar los 500 caracteres."

    if field_errors:
        return JsonResponse(
            {
                "success": False,
                "message": "Revisá los campos marcados antes de guardar.",
                "fieldErrors": field_errors,
            },
            status=400,
        )

    plan = Plan.objects.create(
        title=title,
        date=dt,
        category=category,
        location=location,
        description=description,
        status="planned",
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"“{plan.title}” se sumó a nuestra agenda.",
            "plan": plan.to_dict(),
            **_serialize_state(),
        }
    )


@require_POST
def api_reschedule_plan(request):
    plan_id = (request.POST.get("id") or "").strip()
    date_str = (request.POST.get("date") or "").strip()

    plan = get_object_or_404(Plan, id=plan_id)
    dt = _parse_iso_dt(date_str)
    if not dt:
        return JsonResponse(
            {
                "success": False,
                "message": "Elegí una nueva fecha y hora válidas.",
                "fieldErrors": {"date": "Fecha y hora inválidas."},
            },
            status=400,
        )

    plan.date = dt
    plan.status = "postponed"
    plan.save(update_fields=["date", "status", "updated_at"])

    return JsonResponse(
        {
            "success": True,
            "message": f"Reprogramamos “{plan.title}” para la nueva fecha.",
            "plan": plan.to_dict(),
            **_serialize_state(),
        }
    )


@require_POST
def api_complete_plan(request):
    plan_id = (request.POST.get("id") or "").strip()
    if not plan_id:
        try:
            body = json.loads(request.body.decode("utf-8"))
            plan_id = str(body.get("id", "")).strip()
        except Exception:
            pass

    plan = get_object_or_404(Plan, id=plan_id)
    plan.status = "completed"
    plan.save(update_fields=["status", "updated_at"])

    return JsonResponse(
        {
            "success": True,
            "message": f"¡“{plan.title}” marcado como realizado! Ya podés guardar su recuerdo en el álbum.",
            "plan": plan.to_dict(),
            **_serialize_state(),
        }
    )


@require_POST
def api_create_memory(request):
    plan_id = (request.POST.get("plan") or "").strip()
    happened_at_str = (request.POST.get("happenedAt") or "").strip()
    note = (request.POST.get("note") or "").strip()
    photo = request.FILES.get("photo")

    field_errors = {}
    plan = Plan.objects.filter(id=plan_id).first() if plan_id else None
    if not plan:
        field_errors["plan"] = "Elegí un plan realizado."

    happened_date = parse_date(happened_at_str[:10]) if happened_at_str else None
    if not happened_date:
        field_errors["happenedAt"] = "Elegí una fecha válida para el recuerdo."

    if not note:
        field_errors["note"] = "Escribí una nota o frase sobre este momento."
    elif len(note) > 500:
        field_errors["note"] = "La nota no puede superar los 500 caracteres."

    if photo and photo.size > 8 * 1024 * 1024:
        field_errors["photo"] = "La foto no puede superar los 8 MB."

    if field_errors:
        return JsonResponse(
            {
                "success": False,
                "message": "Revisá los datos del recuerdo antes de guardar.",
                "fieldErrors": field_errors,
            },
            status=400,
        )

    if plan.status != "completed":
        plan.status = "completed"
        plan.save(update_fields=["status", "updated_at"])

    memory = Memory.objects.create(
        plan=plan,
        plan_title=plan.title,
        note=note,
        happened_at=happened_date,
        photo=photo,
        photo_alt=plan.title,
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"Guardamos el recuerdo de “{plan.title}” en nuestro álbum.",
            "memory": memory.to_dict(),
            **_serialize_state(),
        }
    )


@require_GET
def manifest_view(request):
    manifest = {
        "name": "Nosotros — Agenda para parejas",
        "short_name": "Nosotros",
        "description": "Una agenda minimalista para planear citas, reprogramar sin fricción y guardar recuerdos juntos.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f7f3ee",
        "theme_color": "#f7f3ee",
        "lang": "es-AR",
        "icons": [
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": "/favicon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
            },
        ],
    }
    return JsonResponse(
        manifest, content_type="application/manifest+json; charset=utf-8"
    )


@require_GET
def service_worker_view(request):
    sw_path = settings.BASE_DIR / "static" / "sw.js"
    return FileResponse(open(sw_path, "rb"), content_type="application/javascript")


@require_GET
def favicon_view(request):
    fav_path = settings.BASE_DIR / "static" / "img" / "seed" / "favicon.svg"
    return FileResponse(open(fav_path, "rb"), content_type="image/svg+xml")


@require_GET
def download_apk_view(request):
    local_apk = settings.BASE_DIR / "static" / "apk" / "Nosotros-v1.0.0.apk"
    if local_apk.exists():
        return FileResponse(
            open(local_apk, "rb"),
            as_attachment=True,
            filename="Nosotros-v1.0.0.apk",
            content_type="application/vnd.android.package-archive",
        )
    return HttpResponseRedirect(
        "https://github.com/orquera10/app_pareja/releases/latest/download/Nosotros-v1.0.0.apk"
    )


@require_GET
def healthz_view(request):
    return HttpResponse("ok", content_type="text/plain")
