from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import AuditEvent, LabReading, Receipt, Season, User, Version, WorkOrder


@override_settings(SECURE_SSL_REDIRECT=False)
class SeasonReportTests(TestCase):
    report = "/api/v1/reports/seasons/"
    audit = "/api/v1/audit-events"

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=StringIO())
        cls.manager = User.objects.get(username="demo_manager")
        cls.hand = User.objects.get(username="demo_hand")

    def assert_totals(self, year, orders, completed, readings, tonnes):
        response = self.client.get(self.report + str(year))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["year"], year)
        self.assertEqual((data["total_orders"], data["completed_orders"], data["lab_readings"]),
                         (orders, completed, readings))
        self.assertEqual(Decimal(data["net_tonnes_received"]), Decimal(tonnes))
        return data

    def test_fixture_and_independent_receipt_sum(self):
        self.client.force_login(self.manager)
        call_command("seed_demo", stdout=StringIO())
        receipts = Receipt.objects.filter(work_order__lot__season__year=2026)
        self.assertEqual(list(receipts.order_by("net_kg").values_list("net_kg", flat=True)),
                         [1950, 2100, 2350])
        readings = LabReading.objects.order_by("work_order__code", "analyte_id")
        self.assertEqual(list(readings.values_list("work_order__code", "analyte_id")), [
            ("HL-010", "brix"), ("HL-010", "ph"), ("HL-011", "brix"), ("HL-011", "ph"),
        ])
        # A saved weight on an unfinished order must not add fruit to the report.
        Receipt.objects.create(work_order=WorkOrder.objects.get(code="HL-004"), net_kg=150)
        data = self.assert_totals(2026, 12, 8, 4, "6.40")
        self.assertEqual(
            [(r["kind"], r["not_started"], r["in_progress"], r["completed"], r["total"])
             for r in data["by_kind"]],
            [("receive", 1, 0, 3, 4), ("pump_over", 0, 1, 3, 4), ("sample", 1, 1, 2, 4)],
        )
        self.assert_totals(2025, 1, 1, 0, "0.90")
        Season.objects.create(year=2027)
        self.assert_totals(2027, 0, 0, 0, "0")
        self.assert_totals(2028, 0, 0, 0, "0")
        self.assertContains(self.client.get(reverse("season_report", args=[2026])), "6.40 t")
        self.assertContains(self.client.get(reverse("season_report_latest")), "No work orders")

    def test_rehearsal_and_reseed_preserve_work_and_history(self):
        self.client.force_login(self.manager)
        old_order = WorkOrder.objects.get(code="HL-009")
        self.assertEqual((old_order.status, old_order.vessel.code, old_order.version.revision),
                         ("in_progress", "T-04", 1))
        self.assertEqual(old_order.assignee_id, self.hand.pk)

        def post(path, payload, status):
            response = self.client.post("/api/v1" + path, payload, content_type="application/json")
            self.assertEqual(response.status_code, status, response.content[:500])
            return response.json()

        post("/procedures/sample/versions", {"base_revision": 1, "steps": ["Label the tank ID."]}, 201)
        issued = post("/work-orders", {"kind": "sample", "assignee_id": self.hand.pk,
                                      "lot_id": old_order.lot_id}, 201)
        self.assertEqual(issued["revision"], 2)
        self.client.force_login(self.hand)
        post(f"/work-orders/{old_order.pk}/lab-results", {"analyte": "brix", "value": "21.8"}, 201)
        self.client.force_login(self.manager)
        self.assert_totals(2026, 13, 8, 5, "6.40")
        self.client.force_login(self.hand)
        post(f"/work-orders/{old_order.pk}/transitions", {"action": "complete"}, 200)
        self.client.force_login(self.manager)
        self.assert_totals(2026, 13, 9, 5, "6.40")
        events = self.client.get(self.audit).json()
        self.assertEqual(len(events), AuditEvent.objects.count())
        self.assertEqual(events[0]["work_order_id"], old_order.pk)
        self.assertEqual(events[0]["actor_id"], self.hand.pk)
        for event in events:
            targets = [event[key] for key in ("version_id", "work_order_id", "lab_reading_id")]
            self.assertEqual(sum(pk is not None for pk in targets), 1)

        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(self.client.get(self.audit).json(), events)
        old_order.refresh_from_db()
        self.assertEqual(old_order.version.revision, 1)
        self.assertEqual(Version.objects.count(), 4)
        self.assert_totals(2026, 13, 9, 5, "6.40")

    def test_manager_only_access(self):
        page = reverse("season_report", args=[2026])
        for path in (self.report + "2026", self.audit):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(page).status_code, 302)
        self.client.force_login(self.hand)
        for path in (self.report + "2026", self.audit, page):
            self.assertEqual(self.client.get(path).status_code, 403)
