import uuid
from datetime import date
from django.conf import settings
from django.db import models


def _user_display_info(user) -> tuple[str, str]:
    """Return (display_name, initial) for a user using their couple profile if available."""
    if not user:
        return "Nosotros", "N"
    prof = getattr(user, "couple_profile", None)
    name = (
        prof.display_name
        if prof and prof.display_name
        else (user.first_name or user.username.capitalize())
    )
    name = (name or "Nosotros").strip()
    return name, name[0].upper()


class Plan(models.Model):
    CATEGORY_CHOICES = [
        ("date", "Cita"),
        ("food", "Comida"),
        ("trip", "Viaje"),
        ("culture", "Cultura"),
        ("home", "En casa"),
        ("other", "Otro"),
    ]

    STATUS_CHOICES = [
        ("planned", "Planificado"),
        ("postponed", "Reprogramado"),
        ("completed", "Realizado"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField("Nombre del plan", max_length=80)
    description = models.TextField("Detalles", max_length=500, blank=True, default="")
    date = models.DateTimeField("Fecha y hora")
    location = models.CharField("Lugar", max_length=120, blank=True, default="")
    category = models.CharField(
        "Tipo de plan", max_length=20, choices=CATEGORY_CHOICES, default="date"
    )
    status = models.CharField(
        "Estado", max_length=20, choices=STATUS_CHOICES, default="planned"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_plans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "nosotros_plan"
        ordering = ["date"]
        verbose_name = "Plan"
        verbose_name_plural = "Planes"

    def __str__(self) -> str:
        return f"{self.title} ({self.get_status_display()})"

    def to_dict(self) -> dict:
        author_name = ""
        if self.created_by:
            prof = getattr(self.created_by, "couple_profile", None)
            author_name = (
                prof.display_name if prof and prof.display_name else self.created_by.username.capitalize()
            )
        return {
            "id": str(self.id),
            "title": self.title,
            "description": self.description or "",
            "date": self.date.isoformat(),
            "location": self.location or "",
            "category": self.category,
            "status": self.status,
            "authorName": author_name,
        }


class Memory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="memories",
    )
    plan_title = models.CharField("Plan", max_length=120)
    note = models.TextField("Recuerdo", max_length=500)
    happened_at = models.DateField("Fecha del recuerdo")
    photo = models.ImageField("Foto", upload_to="memories/", null=True, blank=True)
    photo_static_url = models.CharField(
        "URL de foto estática/inicial", max_length=300, blank=True, default=""
    )
    photo_alt = models.CharField("Texto alternativo", max_length=160, blank=True, default="")
    external_album_url = models.URLField(
        "Link de Google Photos (opcional)", max_length=500, blank=True, default=""
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_memories",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "nosotros_memory"
        ordering = ["-happened_at", "-created_at"]
        verbose_name = "Recuerdo"
        verbose_name_plural = "Recuerdos"

    def __str__(self) -> str:
        return f"{self.plan_title} — {self.happened_at}"

    def get_photo_url(self) -> str | None:
        if self.photo:
            try:
                return self.photo.url
            except ValueError:
                pass
        if self.photo_static_url:
            return self.photo_static_url
        return None

    def get_photos_list(self) -> list[dict]:
        items = []
        for mp in self.photos.all():
            u = mp.get_url()
            if u:
                items.append(
                    {
                        "id": str(mp.id),
                        "url": u,
                        "alt": mp.alt or self.plan_title,
                    }
                )
        if not items:
            legacy_url = self.get_photo_url()
            if legacy_url:
                items.append(
                    {
                        "id": f"legacy-{self.id}",
                        "url": legacy_url,
                        "alt": self.photo_alt or self.plan_title,
                    }
                )
        return items

    def get_notes_list(self, viewer=None) -> list[dict]:
        viewer_id = viewer.id if (viewer and getattr(viewer, "is_authenticated", False)) else None
        items = []
        for n in self.notes.all():
            name, initial = _user_display_info(n.author)
            items.append(
                {
                    "id": str(n.id),
                    "authorId": n.author_id,
                    "authorName": name,
                    "authorInitial": initial,
                    "text": n.text,
                    "isMine": bool(viewer_id and n.author_id == viewer_id),
                }
            )
        if not items and (self.note or "").strip():
            name, initial = _user_display_info(self.created_by)
            items.append(
                {
                    "id": f"legacy-{self.id}",
                    "authorId": self.created_by_id,
                    "authorName": name,
                    "authorInitial": initial,
                    "text": self.note,
                    "isMine": bool(viewer_id and self.created_by_id == viewer_id),
                }
            )
        return items

    def to_dict(self, viewer=None) -> dict:
        photos_list = self.get_photos_list()
        notes_list = self.get_notes_list(viewer=viewer)
        author_name, author_initial = (
            _user_display_info(self.created_by) if self.created_by else ("", "")
        )
        my_note = next((n["text"] for n in notes_list if n["isMine"]), "")
        return {
            "id": str(self.id),
            "planId": str(self.plan_id) if self.plan_id else None,
            "planTitle": self.plan_title,
            "note": self.note,
            "notes": notes_list,
            "myNote": my_note,
            "happenedAt": f"{self.happened_at.isoformat()}T12:00:00.000Z",
            "authorName": author_name,
            "authorInitial": author_initial,
            "externalAlbumUrl": self.external_album_url or "",
            "photos": photos_list,
            "photo": photos_list[0] if photos_list else None,
        }


class MemoryPhoto(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    memory = models.ForeignKey(
        Memory,
        on_delete=models.CASCADE,
        related_name="photos",
    )
    image = models.ImageField("Foto local", upload_to="memories/", null=True, blank=True)
    remote_url = models.URLField("URL externa (Media Storage)", max_length=500, blank=True, default="")
    remote_key = models.CharField("Clave remota", max_length=300, blank=True, default="")
    static_url = models.CharField("URL estática", max_length=300, blank=True, default="")
    alt = models.CharField("Texto alternativo", max_length=160, blank=True, default="")
    byte_size = models.PositiveIntegerField(default=0)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "nosotros_memory_photo"
        ordering = ["order", "created_at"]
        verbose_name = "Foto de recuerdo"
        verbose_name_plural = "Fotos de recuerdos"

    def get_url(self) -> str | None:
        if self.remote_url:
            return self.remote_url
        if self.image:
            try:
                return self.image.url
            except ValueError:
                pass
        if self.static_url:
            return self.static_url
        return None


class MemoryNote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    memory = models.ForeignKey(
        Memory,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memory_notes",
    )
    text = models.TextField("Qué querés recordar", max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "nosotros_memory_note"
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["memory", "author"], name="unique_memory_note_per_author"
            )
        ]

    def __str__(self) -> str:
        return f"Nota de {self.author} en {self.memory}"


class CoupleProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="couple_profile",
    )
    partner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_profiles",
    )
    display_name = models.CharField("Tu nombre", max_length=60, blank=True, default="")
    nickname_for_partner = models.CharField(
        "Cómo llamás a tu pareja", max_length=60, blank=True, default=""
    )
    anniversary_date = models.DateField(
        "Fecha de aniversario", null=True, blank=True
    )
    favorite_plan_style = models.CharField(
        "Plan favorito",
        max_length=20,
        choices=Plan.CATEGORY_CHOICES,
        default="date",
    )
    favorite_food_or_place = models.CharField(
        "Comida o lugar favorito", max_length=140, blank=True, default=""
    )
    relationship_goal = models.CharField(
        "Qué quieren priorizar juntos", max_length=160, blank=True, default=""
    )
    love_note = models.TextField(
        "Nota para tu pareja", max_length=300, blank=True, default=""
    )
    onboarding_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "nosotros_couple_profile"
        verbose_name = "Perfil de Pareja"
        verbose_name_plural = "Perfiles de Pareja"

    def __str__(self) -> str:
        return f"Perfil de {self.display_name or self.user.username}"

    def effective_anniversary(self) -> date | None:
        if self.anniversary_date:
            return self.anniversary_date
        if self.partner:
            p_prof = getattr(self.partner, "couple_profile", None)
            if p_prof and p_prof.anniversary_date:
                return p_prof.anniversary_date
        return None

    def to_dict(self) -> dict:
        anniv = self.effective_anniversary()
        days_together = (date.today() - anniv).days if anniv else None
        partner_info = None
        if self.partner:
            p_prof = getattr(self.partner, "couple_profile", None)
            partner_info = {
                "username": self.partner.username,
                "displayName": (
                    p_prof.display_name
                    if p_prof and p_prof.display_name
                    else self.partner.username.capitalize()
                ),
                "onboardingCompleted": bool(p_prof and p_prof.onboarding_completed),
                "favoritePlanStyle": p_prof.favorite_plan_style if p_prof else "date",
                "favoriteFoodOrPlace": p_prof.favorite_food_or_place if p_prof else "",
                "relationshipGoal": p_prof.relationship_goal if p_prof else "",
                "loveNote": p_prof.love_note if p_prof else "",
                "nicknameForPartner": p_prof.nickname_for_partner if p_prof else "",
            }
        return {
            "username": self.user.username,
            "displayName": self.display_name or self.user.username.capitalize(),
            "nicknameForPartner": self.nickname_for_partner or "",
            "anniversaryDate": anniv.isoformat() if anniv else "",
            "daysTogether": days_together if days_together is not None and days_together >= 0 else None,
            "favoritePlanStyle": self.favorite_plan_style,
            "favoriteFoodOrPlace": self.favorite_food_or_place or "",
            "relationshipGoal": self.relationship_goal or "",
            "loveNote": self.love_note or "",
            "onboardingCompleted": self.onboarding_completed,
            "partner": partner_info,
        }
