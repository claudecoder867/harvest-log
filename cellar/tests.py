from secrets import token_urlsafe

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import Lot, Season, User, WorkKind
from .services import issue_work_order, publish_version_for_kind, start_work_order


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
)
class CellarAccessTests(TestCase):
    def test_signed_out_user_is_redirected_to_sign_in(self):
        manager_url = reverse("manager")
        response = self.client.get(manager_url)
        self.assertRedirects(response, f"{reverse('login')}?next={manager_url}")

    def test_signed_in_cellar_hand_is_forbidden_from_manager_page(self):
        User = get_user_model()
        password = token_urlsafe(32)
        hand = User.objects.create_user(
            username="test-hand", password=password, role=User.Role.CellarHand,
        )
        client = Client(enforce_csrf_checks=True)
        login_url = reverse("login")
        self.assertEqual(client.get(login_url).status_code, 200)
        response = client.post(login_url, {
            "username": hand.username,
            "password": password,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        })
        self.assertRedirects(response, reverse("home"))
        self.assertEqual(client.get(reverse("manager")).status_code, 403)

    def test_queue_scopes_assignments_and_renders_pinned_steps(self):
        manager = User.objects.create_user("manager", role=User.Role.CellarManager)
        hand = User.objects.create_user("hand", role=User.Role.CellarHand)
        lot = Lot.objects.create(code="PN-9", season=Season.objects.create(year=2026))
        kind = WorkKind.SAMPLE
        publish_version_for_kind(manager, kind, 0, ["Label the bottle"])
        first = issue_work_order(manager, kind=kind, lot_id=lot.pk, assignee_id=hand.pk)
        second = issue_work_order(manager, kind=kind, lot_id=lot.pk, assignee_id=hand.pk)
        start_work_order(hand, second.pk)
        publish_version_for_kind(manager, kind, 1, ["Scan the barcode"])
        third = issue_work_order(manager, kind=kind, lot_id=lot.pk, assignee_id=manager.pk)

        self.client.force_login(hand)
        queue = self.client.get(reverse("home"))
        self.assertTemplateUsed(queue, "cellar/work_queue.html")
        self.assertEqual(list(queue.context["work_orders"]), [second, first])
        self.assertContains(queue, "Label the bottle")
        self.assertNotContains(queue, "Scan the barcode")
        self.assertNotContains(queue, "Issue work order")

        self.client.force_login(manager)
        queue = self.client.get(reverse("home"), follow=True)
        self.assertEqual(list(queue.context["work_orders"]), [second, first, third])
        self.assertContains(queue, "Issue work order")
        self.assertContains(queue, reverse("procedure_editor"))
        self.assertContains(queue, "Scan the barcode")

    def test_editor_requires_manager_and_loads_latest_published_kind(self):
        url = reverse("procedure_editor")
        self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}")
        hand = User.objects.create_user("hand", role=User.Role.CellarHand)
        self.client.force_login(hand)
        self.assertEqual(self.client.get(url).status_code, 403)
        manager = User.objects.create_user("manager", role=User.Role.CellarManager)
        self.client.force_login(manager)
        self.assertContains(self.client.get(url), "Unpublished")
        self.assertEqual(self.client.get(url, {"kind": "unknown"}).status_code, 404)
        publish_version_for_kind(manager, WorkKind.SAMPLE, 0, ["Original instruction"])
        latest = publish_version_for_kind(manager, WorkKind.SAMPLE, 1, ["Updated instruction"])
        response = self.client.get(url, {"kind": WorkKind.SAMPLE})
        self.assertTemplateUsed(response, "cellar/procedure_editor.html")
        self.assertEqual(response.context["published_version"], latest)
        self.assertContains(response, "Updated instruction")
        self.assertNotContains(response, "Original instruction")
