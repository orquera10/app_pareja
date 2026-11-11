from datetime import date
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from .models import Memory, Plan


class NosotrosAgendaTests(TestCase):
    def setUp(self):
        self.client = Client()
        call_command("seed_nosotros")

    def test_seed_data_populated(self):
        self.assertEqual(Plan.objects.count(), 6)
        self.assertEqual(Memory.objects.count(), 3)
        self.assertEqual(Plan.objects.filter(status="completed").count(), 3)

    def test_index_renders_correctly(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nosotros — Agenda para parejas")
        self.assertContains(response, "Cena en nuestro lugar favorito")
        self.assertContains(response, 'closedby="any"')

    def test_create_plan(self):
        response = self.client.post(
            reverse("api_create_plan"),
            {
                "title": "Merienda frente al río",
                "date": "2026-12-05T17:30",
                "category": "date",
                "location": "Vicente López",
                "description": "Llevar mate y medialunas.",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(Plan.objects.count(), 7)

    def test_reschedule_plan(self):
        plan = Plan.objects.filter(status="planned").first()
        response = self.client.post(
            reverse("api_reschedule_plan"),
            {
                "id": str(plan.id),
                "date": "2026-12-12T21:30",
            },
        )
        self.assertEqual(response.status_code, 200)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "postponed")

    def test_complete_plan_and_create_memory(self):
        plan = Plan.objects.filter(status="planned").first()
        res_complete = self.client.post(
            reverse("api_complete_plan"),
            {"id": str(plan.id)},
        )
        self.assertEqual(res_complete.status_code, 200)
        plan.refresh_from_db()
        self.assertEqual(plan.status, "completed")

        # 1x1 PNG bytes
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        photo = SimpleUploadedFile("recuerdo.png", png_bytes, content_type="image/png")
        res_mem = self.client.post(
            reverse("api_create_memory"),
            {
                "plan": str(plan.id),
                "happenedAt": date.today().isoformat(),
                "note": "Fue una noche mágica.",
                "photo": photo,
            },
        )
        self.assertEqual(res_mem.status_code, 200)
        self.assertTrue(res_mem.json()["success"])
        self.assertEqual(Memory.objects.count(), 4)
