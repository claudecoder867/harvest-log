"""Run explicitly with manage.py test qa.browser after installing Playwright Chromium."""
import json
import os
import secrets
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import LiveServerTestCase, override_settings
from playwright.sync_api import expect, sync_playwright


@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ReviewerWalkthrough(LiveServerTestCase):
    def sign_in(self, page, username, password):
        page.goto(self.live_server_url + "/accounts/login/")
        page.keyboard.press("Tab")
        expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
        page.get_by_label("Username").fill(username)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in").click()
        expect(page.get_by_role("heading", name="work queue", exact=False)).to_be_visible()

    def check_viewport(self, page):
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
        self.assertTrue(page.evaluate("""async () => {
            await document.fonts.ready;
            return [...document.fonts].some(f => f.family.includes('Aeonik') && f.status === 'loaded');
        }"""))
        self.assertTrue(page.locator("#primary img").evaluate_all(
            "icons => icons.every(icon => icon.complete && icon.naturalWidth > 0)"
        ))

    def test_two_role_walkthrough(self):
        hand_password = secrets.token_urlsafe(24)
        manager_password = secrets.token_urlsafe(24)
        with patch.dict(os.environ, DEMO_HAND_PASSWORD=hand_password, DEMO_MANAGER_PASSWORD=manager_password):
            call_command("seed_demo", verbosity=0)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            manager = browser.new_page(viewport={"width": 1280, "height": 800})
            hand = browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            for page in (manager, hand):
                page.on("pageerror", lambda error: errors.append(str(error)))
            self.sign_in(manager, "demo_manager", manager_password)
            self.sign_in(hand, "demo_hand", hand_password)
            for page in (manager, hand):
                self.check_viewport(page)
            expect(hand.get_by_role("link", name="Procedure editor")).to_have_count(0)
            self.assertEqual(hand.request.get(self.live_server_url + "/manager/").status, 403)
            schema = manager.request.get(self.live_server_url + "/api/v1/openapi.json").json()
            self.assertEqual(schema, json.loads(Path("openapi.json").read_text()))

            manager.get_by_role("link", name="Season report").click()
            expect(manager.locator("#metrics dd")).to_have_text(["6.40 t", "8 of 12", "4"])
            manager.get_by_role("link", name="Procedure editor").click()
            manager.get_by_label("Procedure", exact=True).select_option("sample")
            manager.get_by_role("button", name="Open procedure").click()
            manager.get_by_role("button", name="Add step").click()
            manager.get_by_role("button", name="Publish revision").click()
            expect(manager.get_by_label("Step 3", exact=True)).to_be_focused()
            expect(manager.locator("#error-summary")).to_be_visible()
            instruction = "Write the tank ID on the sample label."
            manager.get_by_label("Step 3", exact=True).fill(instruction)
            manager.get_by_role("button", name="Publish revision").click()
            manager.get_by_role("button", name="Confirm publication").click()
            expect(manager.locator("#published-revision")).to_have_text("Revision 2")

            manager.get_by_role("link", name="Work queue").click()
            manager.locator("summary").filter(has_text="Issue work order").click()
            manager.get_by_label("Work type").select_option("sample")
            manager.get_by_label("Lot", exact=True).select_option(label="DEMO-PN-2026 · 2026")
            manager.get_by_label("Tank (optional)").select_option(label="T-04")
            manager.get_by_label("Assignee", exact=True).select_option(label="demo_hand")
            order_links = hand.locator('a[aria-label^="Open work order "]')
            link_names = "links => links.map(link => link.getAttribute('aria-label'))"
            original_orders = set(order_links.evaluate_all(link_names))
            with manager.expect_response(lambda r: r.url.endswith("/api/v1/work-orders") and r.request.method == "POST") as issued:
                manager.locator("#issue-form").get_by_role("button", name="Issue work order", exact=True).click()
            self.assertEqual(issued.value.status, 201)

            hand.reload()
            expect(order_links).to_have_count(len(original_orders) + 1)
            new_orders = set(order_links.evaluate_all(link_names)) - original_orders
            self.assertEqual(len(new_orders), 1)
            hand.get_by_role("link", name=new_orders.pop(), exact=True).click()
            expect(hand.get_by_text("Revision 2", exact=True)).to_be_visible()
            expect(hand.get_by_text(instruction)).to_be_visible()
            hand.get_by_role("link", name="Work queue").click()
            hand.get_by_role("link", name="Open work order HL-009").click()
            self.check_viewport(hand)
            expect(hand.get_by_text("Revision 1", exact=True)).to_be_visible()
            expect(hand.get_by_text(instruction)).to_have_count(0)
            hand.get_by_label("Analyte").select_option("brix")
            hand.get_by_label("Measured value").fill("21.8")
            hand.get_by_role("button", name="Add reading").click()
            expect(hand.get_by_text("Recorded a lab reading", exact=True)).to_be_visible()
            hand.get_by_role("button", name="Complete order").click()
            expect(hand.get_by_text("Completed a work order", exact=True)).to_be_visible()
            expect(hand.get_by_role("button", name="Complete order")).to_have_count(0)
            manager.get_by_role("link", name="Season report").click()
            expect(manager.locator("#metrics dd")).to_have_text(["6.40 t", "9 of 13", "5"])
            manager.get_by_role("link", name="2025", exact=True).click()
            expect(manager.locator("#metrics dd")).to_have_text(["0.90 t", "1 of 1", "0"])
            for path in ["/manager/", "/manager/procedures/", "/manager/reports/seasons/2026/"]:
                manager.set_viewport_size({"width": 320, "height": 720})
                manager.goto(self.live_server_url + path)
                self.check_viewport(manager)
            self.assertEqual(errors, [])
            browser.close()
