import uuid
from django.db import models


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
        return {
            "id": str(self.id),
            "title": self.title,
            "description": self.description or "",
            "date": self.date.isoformat(),
            "location": self.location or "",
            "category": self.category,
            "status": self.status,
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

    def to_dict(self) -> dict:
        photo_url = self.get_photo_url()
        return {
            "id": str(self.id),
            "planTitle": self.plan_title,
            "note": self.note,
            "happenedAt": f"{self.happened_at.isoformat()}T12:00:00.000Z",
            "photo": (
                {
                    "url": photo_url,
                    "alt": self.photo_alt or self.plan_title,
                }
                if photo_url
                else None
            ),
        }
