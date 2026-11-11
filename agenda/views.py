import json
from datetime import datetime
from functools import wraps
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import FileResponse, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import CoupleProfile, Memory, MemoryPhoto, Plan
from .storage_client import delete_from_media_storage, upload_to_media_storage


def _get_or_create_profile(user) -> CoupleProfile:
    profile, created = CoupleProfile.objects.get_or_create(
        user=user,
        defaults={
            "display_name": user.first_name or user.username.capitalize(),
        },
    )
    if profile.partner is None:
        User = get_user_model()
        partner_username = (
            "camila"
            if user.username.lower() == "dario"
            else ("dario" if user.username.lower() == "camila" else None)
        )
        if partner_username:
            partner_user = User.objects.filter(username=partner_username).first()
            if partner_user:
                profile.partner = partner_user
                profile.save(update_fields=["partner"])
    return profile


def _serialize_state(user=None):
    plans = [
        p.to_dict()
        for p in Plan.objects.select_related("created_by", "created_by__couple_profile")
        .all()
        .order_by("date")
    ]
    memories = [
        m.to_dict()
        for m in Memory.objects.select_related("created_by", "created_by__couple_profile")
        .prefetch_related("photos")
        .all()
        .order_by("-happened_at", "-created_at")
    ]
    data = {"plans": plans, "memories": memories}
    if user and user.is_authenticated:
        profile = _get_or_create_profile(user)
        data["profile"] = profile.to_dict()
    return data


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


def api_login_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse(
                {
                    "success": False,
                    "message": "Tu sesión expiró. Iniciá sesión nuevamente.",
                },
                status=401,
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        profile = _get_or_create_profile(request.user)
        if not profile.onboarding_completed:
            return redirect("onboarding")
        return redirect("index")

    error_message = ""
    entered_username = ""

    if request.method == "POST":
        entered_username = (request.POST.get("username") or "").strip().lower()
        password = request.POST.get("password") or ""

        if not entered_username or not password:
            error_message = "Ingresá tu usuario y tu contraseña."
        else:
            user = authenticate(request, username=entered_username, password=password)
            if user is not None:
                login(request, user)
                request.session.set_expiry(60 * 60 * 24 * 60)
                profile = _get_or_create_profile(user)
                if not profile.onboarding_completed:
                    return redirect("onboarding")
                next_url = request.POST.get("next") or request.GET.get("next") or "/"
                if not next_url.startswith("/") or next_url.startswith("//"):
                    next_url = "/"
                return redirect(next_url)
            error_message = "El usuario o la contraseña no coinciden. Revisalos e intentá de nuevo."

    return render(
        request,
        "agenda/login.html",
        {
            "error_message": error_message,
            "entered_username": entered_username,
            "next_url": request.GET.get("next", "/"),
        },
    )


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


@ensure_csrf_cookie
@login_required
def onboarding_view(request):
    profile = _get_or_create_profile(request.user)

    if request.method == "GET" and profile.onboarding_completed and request.GET.get("edit") != "1":
        return redirect("index")

    error_message = ""
    field_errors = {}

    if request.method == "POST":
        display_name = (request.POST.get("display_name") or "").strip()
        nickname_for_partner = (request.POST.get("nickname_for_partner") or "").strip()
        anniversary_str = (request.POST.get("anniversary_date") or "").strip()
        favorite_plan_style = (request.POST.get("favorite_plan_style") or "date").strip()
        favorite_food_or_place = (request.POST.get("favorite_food_or_place") or "").strip()
        relationship_goal = (request.POST.get("relationship_goal") or "").strip()
        love_note = (request.POST.get("love_note") or "").strip()
        new_password = request.POST.get("new_password") or ""

        if not display_name:
            field_errors["display_name"] = "Contanos cómo querés que aparezca tu nombre."
        elif len(display_name) > 60:
            field_errors["display_name"] = "El nombre no puede superar los 60 caracteres."

        valid_categories = {c[0] for c in Plan.CATEGORY_CHOICES}
        if favorite_plan_style not in valid_categories:
            favorite_plan_style = "date"

        anniversary_date = None
        if anniversary_str:
            anniversary_date = parse_date(anniversary_str[:10])
            if not anniversary_date:
                field_errors["anniversary_date"] = "Elegí una fecha válida."

        if new_password and len(new_password) < 4:
            field_errors["new_password"] = "Elegí una contraseña de al menos 4 caracteres."

        if not field_errors:
            profile.display_name = display_name
            profile.nickname_for_partner = nickname_for_partner[:60]
            if anniversary_date:
                profile.anniversary_date = anniversary_date
                if profile.partner:
                    p_prof = _get_or_create_profile(profile.partner)
                    if not p_prof.anniversary_date:
                        p_prof.anniversary_date = anniversary_date
                        p_prof.save(update_fields=["anniversary_date", "updated_at"])
            profile.favorite_plan_style = favorite_plan_style
            profile.favorite_food_or_place = favorite_food_or_place[:140]
            profile.relationship_goal = relationship_goal[:160]
            profile.love_note = love_note[:300]
            profile.onboarding_completed = True
            profile.save()

            if display_name:
                request.user.first_name = display_name[:30]
                request.user.save(update_fields=["first_name"])

            if new_password:
                request.user.set_password(new_password)
                request.user.save()
                update_session_auth_hash(request, request.user)

            return redirect("index")

        error_message = "Revisá los campos antes de continuar."

    partner_profile = None
    if profile.partner:
        partner_profile = _get_or_create_profile(profile.partner)

    return render(
        request,
        "agenda/onboarding.html",
        {
            "profile": profile,
            "partner_profile": partner_profile,
            "categories": Plan.CATEGORY_CHOICES,
            "error_message": error_message,
            "field_errors": field_errors,
            "using_default_password": request.user.check_password("123456"),
        },
    )


@ensure_csrf_cookie
@login_required
@require_GET
def index(request):
    profile = _get_or_create_profile(request.user)
    if not profile.onboarding_completed:
        return redirect("onboarding")

    state = _serialize_state(request.user)
    return render(
        request,
        "agenda/index.html",
        {
            "initial_state_json": json.dumps(state, ensure_ascii=False),
            "apk_release_url": "https://github.com/orquera10/app_pareja/releases/latest/download/Nosotros-v1.0.0.apk",
        },
    )


@api_login_required
@require_GET
def api_state(request):
    return JsonResponse({"success": True, **_serialize_state(request.user)})


@api_login_required
@require_POST
def api_change_password(request):
    current_password = request.POST.get("current_password") or ""
    new_password = request.POST.get("new_password") or ""

    field_errors = {}
    if not current_password or not request.user.check_password(current_password):
        field_errors["current_password"] = "Tu contraseña actual no es correcta."

    if len(new_password) < 4:
        field_errors["new_password"] = "La nueva contraseña debe tener al menos 4 caracteres."

    if field_errors:
        return JsonResponse(
            {
                "success": False,
                "message": "No pudimos actualizar la contraseña.",
                "fieldErrors": field_errors,
            },
            status=400,
        )

    request.user.set_password(new_password)
    request.user.save()
    update_session_auth_hash(request, request.user)

    return JsonResponse(
        {
            "success": True,
            "message": "Contraseña actualizada. Ya podés usar tu nueva clave para ingresar.",
        }
    )


@api_login_required
@require_POST
def api_update_profile(request):
    profile = _get_or_create_profile(request.user)
    display_name = (request.POST.get("display_name") or "").strip()
    nickname_for_partner = (request.POST.get("nickname_for_partner") or "").strip()
    anniversary_str = (request.POST.get("anniversary_date") or "").strip()
    favorite_plan_style = (request.POST.get("favorite_plan_style") or profile.favorite_plan_style).strip()
    favorite_food_or_place = (request.POST.get("favorite_food_or_place") or "").strip()
    relationship_goal = (request.POST.get("relationship_goal") or "").strip()
    love_note = (request.POST.get("love_note") or "").strip()

    field_errors = {}
    if not display_name:
        field_errors["display_name"] = "Escribí tu nombre."

    anniversary_date = None
    if anniversary_str:
        anniversary_date = parse_date(anniversary_str[:10])
        if not anniversary_date:
            field_errors["anniversary_date"] = "Fecha de aniversario inválida."

    if field_errors:
        return JsonResponse(
            {
                "success": False,
                "message": "Revisá los datos de tu perfil.",
                "fieldErrors": field_errors,
            },
            status=400,
        )

    valid_categories = {c[0] for c in Plan.CATEGORY_CHOICES}
    profile.display_name = display_name[:60]
    profile.nickname_for_partner = nickname_for_partner[:60]
    if anniversary_date:
        profile.anniversary_date = anniversary_date
    profile.favorite_plan_style = favorite_plan_style if favorite_plan_style in valid_categories else "date"
    profile.favorite_food_or_place = favorite_food_or_place[:140]
    profile.relationship_goal = relationship_goal[:160]
    profile.love_note = love_note[:300]
    profile.onboarding_completed = True
    profile.save()

    return JsonResponse(
        {
            "success": True,
            "message": "Actualizamos tu perfil y los gustos de pareja.",
            **_serialize_state(request.user),
        }
    )


@api_login_required
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
        created_by=request.user,
    )

    return JsonResponse(
        {
            "success": True,
            "message": f"“{plan.title}” se sumó a nuestra agenda.",
            "plan": plan.to_dict(),
            **_serialize_state(request.user),
        }
    )


@api_login_required
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
            **_serialize_state(request.user),
        }
    )


@api_login_required
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
            **_serialize_state(request.user),
        }
    )


def _extract_id(request) -> str:
    item_id = (request.POST.get("id") or "").strip()
    if not item_id and request.body:
        try:
            body = json.loads(request.body.decode("utf-8"))
            item_id = str(body.get("id", "")).strip()
        except Exception:
            pass
    return item_id


@api_login_required
@require_POST
def api_cancel_plan(request):
    plan_id = _extract_id(request)
    plan = get_object_or_404(Plan, id=plan_id)
    title = plan.title
    plan.delete()
    return JsonResponse(
        {
            "success": True,
            "message": f"Cancelamos “{title}” de la agenda.",
            **_serialize_state(request.user),
        }
    )


def _validate_photos(files_list) -> tuple[list, str | None]:
    if len(files_list) > 15:
        return [], "Podés subir hasta 15 fotos por vez."
    for f in files_list:
        if f.size > 12 * 1024 * 1024:
            return [], f"La foto “{f.name}” supera el límite de 12 MB."
    return files_list, None


def _save_memory_photo_files(memory: Memory, files_list: list) -> int:
    start_order = memory.photos.count()
    saved = 0
    for idx, f in enumerate(files_list):
        remote = upload_to_media_storage(f)
        if remote:
            MemoryPhoto.objects.create(
                memory=memory,
                remote_url=remote["url"],
                remote_key=remote["key"],
                alt=memory.plan_title,
                byte_size=int(remote.get("sizeBytes") or f.size or 0),
                order=start_order + idx,
            )
        else:
            f.seek(0)
            MemoryPhoto.objects.create(
                memory=memory,
                image=f,
                alt=memory.plan_title,
                byte_size=f.size or 0,
                order=start_order + idx,
            )
        saved += 1
    return saved


@api_login_required
@require_POST
def api_create_memory(request):
    plan_id = (request.POST.get("plan") or "").strip()
    custom_title = (request.POST.get("customTitle") or "").strip()
    happened_at_str = (request.POST.get("happenedAt") or "").strip()
    note = (request.POST.get("note") or "").strip()
    external_album_url = (request.POST.get("externalAlbumUrl") or "").strip()
    mark_completed = (request.POST.get("markCompleted") or "1").strip() != "0"

    raw_files = list(request.FILES.getlist("photos"))
    single_photo = request.FILES.get("photo")
    if single_photo and single_photo not in raw_files:
        raw_files.append(single_photo)

    field_errors = {}
    plan = Plan.objects.filter(id=plan_id).first() if plan_id else None
    if plan_id and not plan:
        field_errors["plan"] = "No encontramos ese plan."

    plan_title = plan.title if plan else custom_title
    if not plan and not plan_title:
        plan_title = "Momento especial"

    happened_date = parse_date(happened_at_str[:10]) if happened_at_str else None
    if not happened_date:
        field_errors["happenedAt"] = "Elegí una fecha válida para el recuerdo."

    if not note:
        field_errors["note"] = "Escribí una nota o frase sobre este momento."
    elif len(note) > 500:
        field_errors["note"] = "La nota no puede superar los 500 caracteres."

    if external_album_url:
        try:
            URLValidator(schemes=["http", "https"])(external_album_url)
        except ValidationError:
            field_errors["externalAlbumUrl"] = "Pegá un enlace válido (https://photos.app.goo.gl/...)."

    valid_files, photo_err = _validate_photos(raw_files)
    if photo_err:
        field_errors["photos"] = photo_err

    if field_errors:
        return JsonResponse(
            {
                "success": False,
                "message": "Revisá los datos del recuerdo antes de guardar.",
                "fieldErrors": field_errors,
            },
            status=400,
        )

    if plan and mark_completed and plan.status != "completed":
        plan.status = "completed"
        plan.save(update_fields=["status", "updated_at"])

    memory = Memory.objects.create(
        plan=plan,
        plan_title=plan_title[:120],
        note=note,
        happened_at=happened_date,
        external_album_url=external_album_url[:500],
        photo_alt=plan_title[:160],
        created_by=request.user,
    )
    if valid_files:
        _save_memory_photo_files(memory, valid_files)

    return JsonResponse(
        {
            "success": True,
            "message": f"Guardamos el recuerdo de “{memory.plan_title}” en nuestro álbum.",
            "memory": memory.to_dict(),
            **_serialize_state(request.user),
        }
    )


@api_login_required
@require_POST
def api_add_memory_photos(request):
    memory_id = (request.POST.get("id") or "").strip()
    memory = get_object_or_404(Memory, id=memory_id)
    raw_files = list(request.FILES.getlist("photos"))
    if not raw_files:
        return JsonResponse(
            {"success": False, "message": "Elegí al menos una foto para sumar al recuerdo."},
            status=400,
        )
    valid_files, photo_err = _validate_photos(raw_files)
    if photo_err:
        return JsonResponse({"success": False, "message": photo_err}, status=400)

    added = _save_memory_photo_files(memory, valid_files)
    return JsonResponse(
        {
            "success": True,
            "message": f"Sumamos {added} foto{'s' if added != 1 else ''} a “{memory.plan_title}”.",
            "memory": memory.to_dict(),
            **_serialize_state(request.user),
        }
    )


@api_login_required
@require_POST
def api_delete_memory(request):
    memory_id = _extract_id(request)
    memory = get_object_or_404(Memory, id=memory_id)
    title = memory.plan_title

    # Clean up remote storage objects & local ImageField files
    for mp in memory.photos.all():
        if mp.remote_key:
            delete_from_media_storage(mp.remote_key)
        if mp.image:
            mp.image.delete(save=False)
    if memory.photo:
        memory.photo.delete(save=False)

    memory.delete()
    return JsonResponse(
        {
            "success": True,
            "message": f"Eliminamos el recuerdo “{title}”.",
            **_serialize_state(request.user),
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
