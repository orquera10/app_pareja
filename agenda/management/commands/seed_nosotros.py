import uuid
from datetime import date
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime
from agenda.models import CoupleProfile, Memory, Plan


class Command(BaseCommand):
    help = "Seeds initial Nosotros users (dario/camila), couple profiles, plans, and memories."

    def _seed_couple_users(self):
        User = get_user_model()
        dario, dario_created = User.objects.get_or_create(
            username="dario",
            defaults={"first_name": "Darío", "is_active": True},
        )
        if dario_created:
            dario.set_password("123456")
            dario.save()

        camila, camila_created = User.objects.get_or_create(
            username="camila",
            defaults={"first_name": "Camila", "is_active": True},
        )
        if camila_created:
            camila.set_password("123456")
            camila.save()

        dario_profile, _ = CoupleProfile.objects.get_or_create(
            user=dario,
            defaults={
                "partner": camila,
                "display_name": "Darío",
                "onboarding_completed": False,
            },
        )
        if dario_profile.partner_id != camila.id:
            dario_profile.partner = camila
            dario_profile.save(update_fields=["partner"])

        camila_profile, _ = CoupleProfile.objects.get_or_create(
            user=camila,
            defaults={
                "partner": dario,
                "display_name": "Camila",
                "onboarding_completed": False,
            },
        )
        if camila_profile.partner_id != dario.id:
            camila_profile.partner = dario
            camila_profile.save(update_fields=["partner"])

    def add_arguments(self, parser):
        parser.add_argument(
            "--users-only",
            action="store_true",
            help="Only ensure dario/camila users and CoupleProfile exist without creating demo plans or memories.",
        )

    def handle(self, *args, **options):
        self._seed_couple_users()

        if options.get("users_only"):
            self.stdout.write(
                self.style.SUCCESS("Nosotros couple users verified (--users-only).")
            )
            return

        if Plan.objects.exists():
            self.stdout.write(
                self.style.SUCCESS("Nosotros couple users verified and plans already seeded.")
            )
            return

        seed_plans = [
            {
                "id": uuid.UUID("93a3eb3e-6aa2-442e-b064-be9f911e3be0"),
                "title": "Tarde de lluvia en casa",
                "description": "Libro, café y cero horarios.",
                "date": parse_datetime("2026-08-15T18:00:00.000Z"),
                "location": "Casa",
                "category": "home",
                "status": "completed",
            },
            {
                "id": uuid.UUID("a36051fd-50b1-4799-a08f-6f4da33c5d9a"),
                "title": "Desayuno sin apuro",
                "description": "Probamos un café nuevo y nos quedamos hablando toda la mañana.",
                "date": parse_datetime("2026-09-27T13:30:00.000Z"),
                "location": "Villa Crespo",
                "category": "food",
                "status": "completed",
            },
            {
                "id": uuid.UUID("6e325d49-fa5a-4650-b974-df3c5743173c"),
                "title": "Domingo de picnic",
                "description": "Una tarde de sol, frutas y nuestra playlist.",
                "date": parse_datetime("2026-10-18T16:00:00.000Z"),
                "location": "Bosques de Palermo",
                "category": "date",
                "status": "completed",
            },
            {
                "id": uuid.UUID("bf47a69b-277d-4dc2-81c9-4e646910f4c5"),
                "title": "Cena en nuestro lugar favorito",
                "description": "Reservar la mesa junto a la ventana y pedir ese postre que siempre compartimos.",
                "date": parse_datetime("2026-11-13T21:00:00.000Z"),
                "location": "Palermo",
                "category": "food",
                "status": "planned",
            },
            {
                "id": uuid.UUID("8f519a11-7c65-4347-9357-7c6b984fd61e"),
                "title": "Escapada de fin de semana",
                "description": "Dos días sin apuro, caminatas y desayuno largo.",
                "date": parse_datetime("2026-11-21T12:00:00.000Z"),
                "location": "Tigre",
                "category": "trip",
                "status": "planned",
            },
            {
                "id": uuid.UUID("f401a321-3ab7-4242-9214-bf599c722219"),
                "title": "Noche de cine bajo las estrellas",
                "description": "Llevar manta, algo rico y elegir la película entre los dos.",
                "date": parse_datetime("2026-11-28T23:00:00.000Z"),
                "location": "Parque Centenario",
                "category": "date",
                "status": "postponed",
            },
        ]

        plans_by_id = {}
        for item in seed_plans:
            plan = Plan.objects.create(**item)
            plans_by_id[str(plan.id)] = plan

        seed_memories = [
            {
                "id": uuid.UUID("0a695395-03eb-4f24-8bd2-bf5631db4134"),
                "plan": plans_by_id["6e325d49-fa5a-4650-b974-df3c5743173c"],
                "plan_title": "Domingo de picnic",
                "note": "Nos quedamos hasta que bajó el sol y terminamos bailando nuestra canción.",
                "happened_at": date(2026, 10, 18),
                "photo_static_url": "/static/img/seed/picnic-juntos-original.jpg",
                "photo_alt": "Pareja compartiendo un picnic al aire libre",
            },
            {
                "id": uuid.UUID("72a908ca-2aa8-4e20-b6f7-ff311bf1f278"),
                "plan": plans_by_id["a36051fd-50b1-4799-a08f-6f4da33c5d9a"],
                "plan_title": "Desayuno sin apuro",
                "note": "El café estaba buenísimo, pero lo mejor fue perder la noción del tiempo.",
                "happened_at": date(2026, 9, 27),
                "photo_static_url": "/static/img/seed/desayuno-compartido-original.jpg",
                "photo_alt": "Mesa de desayuno compartida por una pareja",
            },
            {
                "id": uuid.UUID("201f31b7-08be-4323-9e42-d064785161f2"),
                "plan": plans_by_id["93a3eb3e-6aa2-442e-b064-be9f911e3be0"],
                "plan_title": "Tarde de lluvia en casa",
                "note": "Una tarde simple que terminó siendo de nuestras favoritas.",
                "happened_at": date(2026, 8, 15),
                "photo_static_url": "/static/img/seed/manana-juntos-original.jpg",
                "photo_alt": "Pareja abrazada en una mañana tranquila en casa",
            },
        ]

        for mem in seed_memories:
            Memory.objects.create(**mem)

        self.stdout.write(
            self.style.SUCCESS("Successfully seeded users, 6 plans, and 3 memories for Nosotros.")
        )
