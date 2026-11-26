import json
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings

from .models import AuditEvent, LabReading, Lot, Receipt, Season, User, WorkOrder
from .services import complete_work_order, issue_work_order, start_work_order


@override_settings(SECURE_SSL_REDIRECT=False)
class CellarApiTests(TestCase):
    root = "/api/v1"
    versions = "/procedures/sample/versions"
    orders = "/work-orders"

    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user("manager", role=User.Role.CellarManager)
        cls.hand = User.objects.create_user("hand", role=User.Role.CellarHand)
        cls.other = User.objects.create_user("other", role=User.Role.CellarHand)
        cls.lot = Lot.objects.create(code="PN-1", season=Season.objects.create(year=2026))

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/accounts/login/")

    def post(self, path, data, status):
        response = self.client.post(
            self.root + path, data, content_type="application/json",
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, status, response.content[:1500])
        return response.json()

    def publish(self, kind="sample", base=0, instruction="Clean valve"):
        return self.post(f"/procedures/{kind}/versions", {
            "base_revision": base, "steps": [instruction],
        }, 201)

    def issue_data(self, kind="sample", **extra):
        return {"kind": kind, "lot_id": self.lot.pk, "assignee_id": self.hand.pk, **extra}

    def issue(self, kind="sample"):
        return self.post(self.orders, self.issue_data(kind), 201)

    def test_schema_and_session_authentication(self):
        self.assertEqual(self.client.get(self.root + self.orders).status_code, 401)
        self.assertEqual(self.client.get(self.root + self.versions).status_code, 401)
        self.assertEqual(self.client.get(self.root + "/docs").status_code, 200)
        schema = self.client.get(self.root + "/openapi.json").json()
        self.assertEqual(schema, json.loads((Path(settings.BASE_DIR) / "openapi.json").read_text()))
        for operations in schema["paths"].values():
            for operation in operations.values():
                scheme = next(iter(operation["security"][0]))
                self.assertEqual(schema["components"]["securitySchemes"][scheme]["in"], "cookie")

        self.client.force_login(self.manager)
        self.publish()
        order = self.issue()
        detail = f"{self.orders}/{order['id']}"
        for path, body in (
            (self.orders, self.issue_data()), (detail + "/transitions", {"action": "start"}),
            (detail + "/lab-results", {"analyte": "ph", "value": "3.42"}),
            (self.versions, {"base_revision": 1, "steps": ["Changed"]}),
        ):
            with self.subTest(path=path):
                response = self.client.post(self.root + path, body, content_type="application/json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["code"], "forbidden")
        self.assertEqual(AuditEvent.objects.count(), 2)

    def test_order_visibility_and_manager_only_actions(self):
        self.client.force_login(self.manager)
        self.publish()
        owned = self.issue()
        other = self.post(self.orders, self.issue_data(assignee_id=self.other.pk), 201)
        self.assertEqual(len(self.client.get(self.root + self.orders).json()), 2)
        self.client.force_login(self.hand)
        self.assertEqual([row["id"] for row in self.client.get(self.root + self.orders).json()], [owned["id"]])
        self.post(self.orders, self.issue_data(), 403)
        self.post(self.versions, {"base_revision": 1, "steps": ["Change"]}, 403)
        self.assertEqual(self.client.get(self.root + self.versions).status_code, 403)
        private = f"{self.orders}/{other['id']}"
        missing = self.client.get(self.root + self.orders + "/987654321")
        response = self.client.get(self.root + private)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), missing.json())
        self.post(private + "/transitions", {"action": "start"}, 404)
        self.post(private + "/lab-results", {"analyte": "ph", "value": "3.4"}, 404)
        self.assertEqual(AuditEvent.objects.count(), 3)

    def test_publication_pinning_and_sample_lifecycle(self):
        self.client.force_login(self.manager)
        self.publish()
        first = self.issue()
        self.publish(base=1, instruction="Label bottle")
        second = self.issue()
        self.assertEqual([first["revision"], second["revision"]], [1, 2])
        self.post(self.versions, {"base_revision": 1, "steps": ["Stale"]}, 409)
        history = self.client.get(self.root + self.versions).json()
        self.assertEqual([v["revision"] for v in history], [2, 1])

        self.client.force_login(self.hand)
        detail = f"{self.orders}/{first['id']}"
        transition = detail + "/transitions"
        self.post(transition, {"action": "complete"}, 409)
        self.post(transition, {"action": "start"}, 200)
        self.post(transition, {"action": "start"}, 409)
        self.post(transition, {"action": "complete"}, 409)
        invalid = self.post(detail + "/lab-results", {"analyte": "unknown", "value": 3}, 422)
        self.assertEqual(invalid["field"], "analyte")
        # A binary float parser would round this to 3.4 before field validation.
        response = self.client.post(
            self.root + detail + "/lab-results", '{"analyte":"ph","value":3.40000000000000001}',
            content_type="application/json", HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["field"], "value")
        self.assertFalse(LabReading.objects.exists())
        self.post(detail + "/lab-results", {"analyte": "brix", "value": "21.375"}, 201)
        self.post(transition, {"action": "complete"}, 200)
        self.post(transition, {"action": "complete"}, 409)
        result = self.client.get(self.root + detail).json()
        self.assertEqual(result["version"]["steps"], [{"position": 1, "instruction": "Clean valve"}])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["lab_readings"][0]["recorded_by"], self.hand.pk)
        self.assertEqual(result["lab_readings"][0]["unit"], "°Brix")
        self.assertEqual([e["action"] for e in result["timeline"]], [
            "work_order_issued", "work_order_started", "lab_reading_recorded", "work_order_completed",
        ])

    def test_capture_for_both_non_sample_kinds(self):
        self.client.force_login(self.manager)
        for kind, field, value in (("receive", "net_kg", "123.125"), ("pump_over", "elapsed_minutes", 12)):
            with self.subTest(kind=kind):
                self.publish(kind)
                order = self.issue(kind)
                detail = f"{self.orders}/{order['id']}"
                transition = detail + "/transitions"
                self.post(transition, {"action": "start"}, 200)
                self.post(transition, {"action": "complete", field: 0}, 422)
                self.post(transition, {"action": "complete"}, 409)
                self.post(transition, {"action": "complete", field: value}, 200)
                self.assertEqual(self.client.get(self.root + detail).json()[field], value)

    def test_input_validation_precedes_writes(self):
        self.client.force_login(self.manager)
        self.post(self.orders, self.issue_data(), 409)
        invalid = self.post(self.versions, {"base_revision": 0, "steps": ["  "]}, 422)
        self.assertTrue(invalid["field"].endswith("steps.0"), invalid)
        self.publish()
        for extra in ({"version_id": 1}, {"assignee_id": 9999999}, {"lot_id": 9999999},
                      {"vessel_id": 9999999}, {"code": "bad\x00code"}):
            self.post(self.orders, self.issue_data(**extra), 422)
        self.post(self.orders, self.issue_data(code="HL-001"), 201)
        self.post(self.orders, self.issue_data(code="HL-001"), 409)
        self.assertEqual(WorkOrder.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 2)

    def test_issue_start_and_measurement_roll_back_if_audit_fails(self):
        self.client.force_login(self.manager)
        self.publish("receive")
        data = self.issue_data("receive")
        with patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("audit")):
            with self.assertRaises(IntegrityError):
                issue_work_order(self.manager, **data)
        self.assertFalse(WorkOrder.objects.exists())
        order = issue_work_order(self.manager, **data)
        with patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("audit")):
            with self.assertRaises(IntegrityError):
                start_work_order(self.hand, order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, "not_started")
        start_work_order(self.hand, order.pk)
        with patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("audit")):
            with self.assertRaises(IntegrityError):
                complete_work_order(self.hand, order.pk, net_kg="1.230")
        order.refresh_from_db()
        self.assertEqual(order.status, "in_progress")
        self.assertFalse(Receipt.objects.exists())
