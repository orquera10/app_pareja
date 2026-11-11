from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from .models import CoupleProfile, Memory, Plan

User = get_user_model()


class NosotrosAgendaTests(TestCase):
    def setUp(self):
        self.client = Client()
        call_command("seed_nosotros")

    def _complete_onboarding_for(self, username, display_name, new_password=""):
        payload = {
            "display_name": display_name,
            "nickname_for_partner": "Amor",
            "anniversary_date": "2024-05-18",
            "favorite_plan_style": "date",
            "favorite_food_or_place": "Helado artesanal",
            "relationship_goal": "Tener una cita por semana",
            "love_note": "Nos vemos esta noche",
        }
        if new_password:
            payload["new_password"] = new_password
        return self.client.post("/onboarding/", payload)

    def test_seed_data_and_couple_users_populated(self):
        self.assertEqual(Plan.objects.count(), 6)
        self.assertEqual(Memory.objects.count(), 3)
        self.assertEqual(Plan.objects.filter(status="completed").count(), 3)

        dario = User.objects.get(username="dario")
        camila = User.objects.get(username="camila")
        self.assertTrue(dario.check_password("123456"))
        self.assertTrue(camila.check_password("123456"))
        self.assertEqual(dario.couple_profile.partner, camila)
        self.assertEqual(camila.couple_profile.partner, dario)
        self.assertFalse(dario.couple_profile.onboarding_completed)
        self.assertFalse(camila.couple_profile.onboarding_completed)

    def test_unauthenticated_access_is_protected(self):
        index_res = self.client.get(reverse("index"))
        self.assertEqual(index_res.status_code, 302)
        self.assertIn("/login/", index_res["Location"])

        api_res = self.client.get(reverse("api_state"))
        self.assertEqual(api_res.status_code, 401)
        self.assertFalse(api_res.json()["success"])

        login_page = self.client.get("/login/")
        self.assertEqual(login_page.status_code, 200)
        self.assertContains(login_page, 'autocomplete="username"')
        self.assertContains(login_page, 'autocomplete="current-password"')

        health_res = self.client.get(reverse("healthz"))
        self.assertEqual(health_res.status_code, 200)

    def test_first_login_redirects_to_onboarding_for_dario_and_camila(self):
        # Dario signs in for the first time -> redirected to onboarding
        res = self.client.post("/login/", {"username": "dario", "password": "123456"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], "/onboarding/")

        # Index stays gated until onboarding finishes
        gated = self.client.get(reverse("index"))
        self.assertEqual(gated.status_code, 302)
        self.assertEqual(gated["Location"], "/onboarding/")

        # Onboarding page renders autofill & wizard step markup
        onb_page = self.client.get("/onboarding/")
        self.assertEqual(onb_page.status_code, 200)
        self.assertContains(onb_page, 'autocomplete="new-password"')
        self.assertContains(onb_page, "Camila")

        # Complete onboarding for Dario
        done = self._complete_onboarding_for("dario", "Darío")
        self.assertEqual(done.status_code, 302)
        self.assertEqual(done["Location"], "/")

        dario_prof = CoupleProfile.objects.get(user__username="dario")
        self.assertTrue(dario_prof.onboarding_completed)
        self.assertEqual(dario_prof.nickname_for_partner, "Amor")

        # Now Camila signs in for the first time with a separate client -> sees onboarding too
        camila_client = Client()
        res_camila = camila_client.post("/login/", {"username": "camila", "password": "123456"})
        self.assertEqual(res_camila.status_code, 302)
        self.assertEqual(res_camila["Location"], "/onboarding/")

        done_camila = camila_client.post(
            "/onboarding/",
            {
                "display_name": "Cami",
                "nickname_for_partner": "Gordo",
                "anniversary_date": "",
                "favorite_plan_style": "food",
                "favorite_food_or_place": "Café y medialunas",
                "relationship_goal": "Viajar al sur",
                "love_note": "Te quiero mucho",
            },
        )
        self.assertEqual(done_camila.status_code, 302)
        self.assertEqual(done_camila["Location"], "/")

        camila_prof = CoupleProfile.objects.get(user__username="camila")
        self.assertTrue(camila_prof.onboarding_completed)
        # Camila inherits Dario's anniversary via effective_anniversary()
        self.assertEqual(camila_prof.effective_anniversary(), date(2024, 5, 18))

    def test_index_profile_and_password_change(self):
        self.client.force_login(User.objects.get(username="dario"))
        self._complete_onboarding_for("dario", "Darío")

        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nosotros — Agenda para parejas")
        self.assertContains(response, "Cena en nuestro lugar favorito")
        self.assertContains(response, 'closedby="any"')
        self.assertContains(response, 'id="dialog-account"')

        # Update profile via API
        prof_res = self.client.post(
            "/api/account/profile/",
            {
                "display_name": "Dari",
                "nickname_for_partner": "Mi vida",
                "anniversary_date": "2024-06-01",
                "favorite_plan_style": "trip",
                "favorite_food_or_place": "Tigre",
                "relationship_goal": "Escapada mensual",
                "love_note": "Planazo el finde",
            },
        )
        self.assertEqual(prof_res.status_code, 200)
        self.assertTrue(prof_res.json()["success"])
        self.assertEqual(prof_res.json()["profile"]["displayName"], "Dari")

        # Wrong current password rejected
        bad_pw = self.client.post(
            "/api/account/password/",
            {"current_password": "wrong", "new_password": "nueva123"},
        )
        self.assertEqual(bad_pw.status_code, 400)
        self.assertFalse(bad_pw.json()["success"])

        # Valid password change succeeds and keeps session authenticated
        good_pw = self.client.post(
            "/api/account/password/",
            {"current_password": "123456", "new_password": "nueva123"},
        )
        self.assertEqual(good_pw.status_code, 200)
        self.assertTrue(good_pw.json()["success"])
        self.assertEqual(self.client.get(reverse("index")).status_code, 200)

        # Logout & login with new password works
        self.client.post("/logout/")
        relogin = self.client.post("/login/", {"username": "dario", "password": "nueva123"})
        self.assertEqual(relogin.status_code, 302)
        self.assertEqual(relogin["Location"], "/")

    def test_create_reschedule_complete_plan_and_create_memory(self):
        self.client.force_login(User.objects.get(username="dario"))
        self._complete_onboarding_for("dario", "Darío")

        res_create = self.client.post(
            reverse("api_create_plan"),
            {
                "title": "Merienda frente al río",
                "date": "2026-12-05T17:30",
                "category": "date",
                "location": "Vicente López",
                "description": "Llevar mate y medialunas.",
            },
        )
        self.assertEqual(res_create.status_code, 200)
        data = res_create.json()
        self.assertTrue(data["success"])
        self.assertEqual(Plan.objects.count(), 7)
        created_plan = Plan.objects.get(title="Merienda frente al río")
        self.assertEqual(created_plan.created_by.username, "dario")

        res_reschedule = self.client.post(
            reverse("api_reschedule_plan"),
            {"id": str(created_plan.id), "date": "2026-12-12T21:30"},
        )
        self.assertEqual(res_reschedule.status_code, 200)
        created_plan.refresh_from_db()
        self.assertEqual(created_plan.status, "postponed")

        # Cannot attach memory before plan is completed
        res_mem_before_complete = self.client.post(
            reverse("api_create_memory"),
            {
                "plan": str(created_plan.id),
                "happenedAt": date.today().isoformat(),
                "note": "Todavía no pasó.",
            },
        )
        self.assertEqual(res_mem_before_complete.status_code, 400)
        self.assertFalse(res_mem_before_complete.json()["success"])
        self.assertIn("plan", res_mem_before_complete.json()["fieldErrors"])

        res_complete = self.client.post(
            reverse("api_complete_plan"),
            {"id": str(created_plan.id)},
        )
        self.assertEqual(res_complete.status_code, 200)
        created_plan.refresh_from_db()
        self.assertEqual(created_plan.status, "completed")

        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        photo1 = SimpleUploadedFile("recuerdo1.png", png_bytes, content_type="image/png")
        photo2 = SimpleUploadedFile("recuerdo2.png", png_bytes, content_type="image/png")
        res_mem = self.client.post(
            reverse("api_create_memory"),
            {
                "plan": str(created_plan.id),
                "happenedAt": date.today().isoformat(),
                "note": "Fue una noche mágica.",
                "externalAlbumUrl": "https://photos.app.goo.gl/ejemplo123",
                "photos": [photo1, photo2],
            },
        )
        self.assertEqual(res_mem.status_code, 200)
        mem_json = res_mem.json()
        self.assertTrue(mem_json["success"])
        self.assertEqual(Memory.objects.count(), 4)
        self.assertEqual(len(mem_json["memory"]["photos"]), 2)
        self.assertEqual(mem_json["memory"]["externalAlbumUrl"], "https://photos.app.goo.gl/ejemplo123")

        # Add another photo to existing memory carousel
        photo3 = SimpleUploadedFile("recuerdo3.png", png_bytes, content_type="image/png")
        res_add = self.client.post(
            reverse("api_add_memory_photos"),
            {"id": mem_json["memory"]["id"], "photos": [photo3]},
        )
        self.assertEqual(res_add.status_code, 200)
        self.assertEqual(len(res_add.json()["memory"]["photos"]), 3)

        # Delete a single photo cleanly (including remote_key cleanup on storage server)
        second_photo_id = res_add.json()["memory"]["photos"][1]["id"]
        from .models import MemoryPhoto
        mp_target = MemoryPhoto.objects.get(id=second_photo_id)
        mp_target.remote_key = "nosotros/2026-11/sample-delete.webp"
        mp_target.save(update_fields=["remote_key"])

        with patch("agenda.views.delete_from_media_storage", return_value=True) as mock_remote_del:
            res_del_photo = self.client.post(
                reverse("api_delete_memory_photo"),
                {"memoryId": mem_json["memory"]["id"], "photoId": second_photo_id},
            )
        self.assertEqual(res_del_photo.status_code, 200)
        self.assertTrue(res_del_photo.json()["success"])
        mock_remote_del.assert_called_once_with("nosotros/2026-11/sample-delete.webp")
        self.assertEqual(len(res_del_photo.json()["memory"]["photos"]), 2)
        self.assertEqual(Memory.objects.count(), 4)

        # Delete memory cleanly
        res_del_mem = self.client.post(
            reverse("api_delete_memory"),
            {"id": mem_json["memory"]["id"]},
        )
        self.assertEqual(res_del_mem.status_code, 200)
        self.assertEqual(Memory.objects.count(), 3)

        # Cancel a planned date cleanly
        res_cancel = self.client.post(
            reverse("api_cancel_plan"),
            {"id": str(created_plan.id)},
        )
        self.assertEqual(res_cancel.status_code, 200)
        self.assertEqual(Plan.objects.count(), 6)
