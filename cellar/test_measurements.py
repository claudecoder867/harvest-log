from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, connections, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from ninja.errors import HttpError

from config.urls import api

from .models import (
    Analyte, AuditEvent, LabReading, Lot, Procedure, PumpOver, Receipt, Season, User,
    WorkKind, WorkOrder,
)
from .services import complete_work_order, publish_procedure_version, record_lab_reading


def make_order(kind, manager, hand, lot):
    procedure = Procedure.objects.create(kind=kind)
    version = publish_procedure_version(manager, procedure.pk, 0, ["Follow the cellar procedure."])
    return WorkOrder.objects.create(
        code=f"HL-{kind}", lot=lot, assignee=hand, version=version,
        status=WorkOrder.Status.IN_PROGRESS,
    )


class MeasurementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(username="manager", role=User.Role.CellarManager)
        cls.hand = User.objects.create_user(username="hand", role=User.Role.CellarHand)
        cls.other_hand = User.objects.create_user(username="other", role=User.Role.CellarHand)
        lot = Lot.objects.create(code="PN-01", season=Season.objects.create(year=2026))
        cls.sample = make_order(WorkKind.SAMPLE, cls.manager, cls.hand, lot)
        cls.pump = make_order(WorkKind.PUMP_OVER, cls.manager, cls.hand, lot)
        cls.receive = make_order(WorkKind.RECEIVE, cls.manager, cls.hand, lot)

    def assert_rejected(self, status, operation, user, order, *args):
        order.refresh_from_db()
        before = order.status, AuditEvent.objects.count(), LabReading.objects.count()
        with self.assertRaises(HttpError) as rejected:
            operation(user, order.pk, *args)
        response = api.on_exception(RequestFactory().post("/"), rejected.exception)
        self.assertEqual(response.status_code, status)
        order.refresh_from_db()
        self.assertEqual((order.status, AuditEvent.objects.count(), LabReading.objects.count()), before)

    def test_sample_without_reading_returns_409(self):
        self.assert_rejected(409, complete_work_order, self.hand, self.sample)

    def test_lab_reading_on_pump_over_returns_409(self):
        self.assert_rejected(409, record_lab_reading, self.hand, self.pump, "ph", "3.42")

    def test_second_completion_returns_409_and_preserves_the_first_event(self):
        PumpOver.objects.create(work_order=self.pump, elapsed_minutes=12)
        complete_work_order(self.hand, self.pump.pk)
        self.assert_rejected(409, complete_work_order, self.manager, self.pump)
        self.assertEqual(self.pump.audit_events.get().actor, self.hand)

    def test_valid_completion_preserves_the_pinned_revision_and_records_the_change(self):
        pinned_version = self.sample.version
        publish_procedure_version(self.manager, pinned_version.procedure_id, 1, ["Label the tank."])
        ph = Analyte.objects.get(code="ph")
        self.assertEqual(ph.unit, "")
        first = record_lab_reading(self.hand, self.sample.pk, ph, Decimal("3.42"))
        second = record_lab_reading(self.manager, self.sample.pk, "brix", "21.80")
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.value, Decimal("3.42"))
        self.assertEqual(second.analyte.unit, "°Brix")
        self.assertEqual(second.value, Decimal("21.80"))
        for reading, actor, value in ((first, self.hand, "3.42"), (second, self.manager, "21.80")):
            event = reading.creation_event
            self.assertEqual((event.actor, event.action), (actor, AuditEvent.Action.LAB_RECORDED))
            self.assertEqual(event.changes, {"analyte": reading.analyte_id, "value": value})
            self.assertIsNotNone(event.occurred_at)

        complete_work_order(self.hand, self.sample.pk)
        self.sample.refresh_from_db()
        self.assertEqual(self.sample.status, WorkOrder.Status.COMPLETED)
        self.assertEqual(self.sample.version_id, pinned_version.pk)
        self.assertEqual(self.sample.version.steps.get().instruction, "Follow the cellar procedure.")
        event = self.sample.audit_events.get()
        self.assertEqual((event.actor, event.action), (self.hand, AuditEvent.Action.COMPLETED))
        self.assertEqual(event.changes, {"status": {"from": "in_progress", "to": "completed"}})
        self.assertIsNotNone(event.occurred_at)
        self.assert_rejected(409, record_lab_reading, self.hand, self.sample, "ph", "3.5")

    def test_each_non_sample_order_requires_its_measurement(self):
        for order, model, fields in (
            (self.receive, Receipt, {"net_kg": Decimal("1950.125")}),
            (self.pump, PumpOver, {"elapsed_minutes": 15}),
        ):
            with self.subTest(kind=order.kind):
                self.assert_rejected(409, complete_work_order, self.hand, order)
                model.objects.create(work_order=order, **fields)
                complete_work_order(self.manager, order.pk)
                order.refresh_from_db()
                self.assertEqual(order.status, WorkOrder.Status.COMPLETED)
                self.assertEqual(order.audit_events.get().actor, self.manager)

    def test_unstarted_order_rejects_both_operations(self):
        self.sample.status = WorkOrder.Status.NOT_STARTED
        self.sample.save(update_fields=["status"])
        self.assert_rejected(409, record_lab_reading, self.hand, self.sample, "ph", "3.4")
        self.assert_rejected(409, complete_work_order, self.hand, self.sample)

    def test_assignment_and_authentication_checked_before_state_or_kind(self):
        for operation, args in ((complete_work_order, ()), (record_lab_reading, ("ph", "3.42"))):
            with self.subTest(operation=operation.__name__):
                self.assert_rejected(404, operation, self.other_hand, self.pump, *args)
                self.assert_rejected(401, operation, AnonymousUser(), self.pump, *args)
                self.manager.is_active = False
                self.assert_rejected(403, operation, self.manager, self.pump, *args)

    def test_invalid_readings_leave_order_and_audit_untouched(self):
        for analyte, value in (
            ("unknown", "3.42"), ("ph", None), ("ph", "NaN"), ("ph", "Infinity"),
            ("ph", "3.12345"), ("ph", "123456789"),
        ):
            with self.subTest(analyte=analyte, value=value):
                self.assert_rejected(422, record_lab_reading, self.hand, self.sample, analyte, value)

    def test_audit_failure_rolls_back_reading_and_completion(self):
        initial_events = AuditEvent.objects.count()
        with patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("audit failed")):
            with self.assertRaisesMessage(IntegrityError, "audit failed"):
                record_lab_reading(self.hand, self.sample.pk, "ph", "3.42")
        self.assertFalse(self.sample.lab_readings.exists())
        self.assertEqual(AuditEvent.objects.count(), initial_events)

        record_lab_reading(self.hand, self.sample.pk, "ph", "3.42")
        with patch.object(AuditEvent.objects, "create", side_effect=IntegrityError("audit failed")):
            with self.assertRaisesMessage(IntegrityError, "audit failed"):
                complete_work_order(self.hand, self.sample.pk)
        self.sample.refresh_from_db()
        self.assertEqual(self.sample.status, WorkOrder.Status.IN_PROGRESS)
        self.assertFalse(self.sample.audit_events.exists())
        self.assertEqual(AuditEvent.objects.count(), initial_events + 1)

    def test_database_enforces_measurement_kind_quantity_and_cardinality(self):
        for model, order, field in (
            (Receipt, self.receive, "net_kg"), (PumpOver, self.pump, "elapsed_minutes"),
        ):
            with self.subTest(model=model.__name__):
                for invalid_order, quantity in ((self.sample, 1), (order, 0), (order, -1)):
                    with self.assertRaises(IntegrityError), transaction.atomic():
                        model.objects.create(work_order=invalid_order, **{field: quantity})
                model.objects.create(work_order=order, **{field: 1})
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(work_order=order, **{field: 2})
        with self.assertRaises(IntegrityError), transaction.atomic():
            LabReading.objects.create(work_order=self.pump, analyte_id="ph", value=3)


class ConcurrentCompletionTests(TransactionTestCase):
    def test_two_completions_produce_one_event_and_one_conflict(self):
        manager = User.objects.create_user(username="manager", role=User.Role.CellarManager)
        hand = User.objects.create_user(username="hand", role=User.Role.CellarHand)
        lot = Lot.objects.create(code="PN-01", season=Season.objects.create(year=2026))
        order = make_order(WorkKind.RECEIVE, manager, hand, lot)
        Receipt.objects.create(work_order=order, net_kg=100)
        ready = Barrier(2)

        def complete(actor):
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                ready.wait(timeout=10)
                complete_work_order(actor, order.pk)
                return actor.pk, 200
            except HttpError as exc:
                return actor.pk, exc.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete, actor) for actor in (hand, manager)]
            outcomes = [future.result(timeout=15) for future in futures]
        self.assertCountEqual([status for _, status in outcomes], [200, 409])
        self.assertIn((order.audit_events.get().actor_id, 200), outcomes)
        order.refresh_from_db()
        self.assertEqual(order.status, WorkOrder.Status.COMPLETED)
