from secrets import token_urlsafe

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse


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
