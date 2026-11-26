from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from ninja.errors import HttpError

from .models import AuditEvent, Lot, Procedure, Season, Step, User, Version, Vessel, WorkOrder
from .services import publish_procedure_version


class PublicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(username="manager", role=User.Role.CellarManager)
        cls.hand = User.objects.create_user(username="hand", role=User.Role.CellarHand)
        cls.procedure = Procedure.objects.create(kind=Procedure.Kind.SAMPLE)
        cls.lot = Lot.objects.create(code="PN-01", season=Season.objects.create(year=2026))
        cls.tank = Vessel.objects.create(code="T-04")

    def issue_sample(self, code, version):
        return WorkOrder.objects.create(
            code=code, lot=self.lot, vessel=self.tank, assignee=self.hand, version=version,
        )

    def test_old_order_keeps_its_original_instructions_after_publication(self):
        original = ["Rinse the sample valve.", "Fill the sample bottle."]
        revised = ["Label the bottle with the tank ID.", "Fill the labelled bottle."]
        v1 = publish_procedure_version(self.manager, self.procedure.pk, 0, original)
        first_order = self.issue_sample("HL-009", v1)
        WorkOrder.objects.filter(pk=first_order.pk).update(status=WorkOrder.Status.IN_PROGRESS)

        v2 = publish_procedure_version(self.manager, self.procedure.pk, 1, revised)
        second_order = self.issue_sample("HL-013", v2)

        def snapshot(order_id):
            order = WorkOrder.objects.select_related("version").get(pk=order_id)
            steps = list(order.version.steps.values_list("position", "instruction"))
            return (order.version_id, order.version.revision, steps)

        self.assertNotEqual(v1.pk, v2.pk)
        self.assertEqual(snapshot(first_order.pk), (v1.pk, 1, list(enumerate(original, 1))))
        self.assertEqual(snapshot(second_order.pk), (v2.pk, 2, list(enumerate(revised, 1))))
        first_order.refresh_from_db()
        self.assertEqual(first_order.status, WorkOrder.Status.IN_PROGRESS)
        self.assertEqual(first_order.kind, Procedure.Kind.SAMPLE)
        self.assertEqual(
            list(AuditEvent.objects.order_by("version__revision").values_list(
                "version_id", "actor_id", "action",
            )),
            [(v1.pk, self.manager.pk, AuditEvent.Action.PUBLISHED),
             (v2.pk, self.manager.pk, AuditEvent.Action.PUBLISHED)],
        )

    def test_cellar_hand_cannot_publish_even_with_staff_and_superuser_flags(self):
        self.hand.is_staff = self.hand.is_superuser = True
        with self.assertRaises(HttpError) as denied:
            publish_procedure_version(self.hand, self.procedure.pk, 0, ["Collect the sample."])
        self.assertEqual(denied.exception.status_code, 403)
        self.assertFalse(Version.objects.exists())
        self.assertFalse(Step.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_stale_editor_leaves_published_text_and_history_intact(self):
        publish_procedure_version(self.manager, self.procedure.pk, 0, ["  Label the bottle.  "])
        with self.assertRaises(HttpError) as stale:
            publish_procedure_version(self.manager, self.procedure.pk, 0, ["Conflicting edit."])
        self.assertEqual(stale.exception.status_code, 409)
        self.assertEqual(Version.objects.count(), 1)
        self.assertEqual(Step.objects.get().instruction, "Label the bottle.")
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_invalid_steps_leave_no_partial_publication(self):
        for steps in ([], ["Rinse.", "  "], ["x" * 241], ["x"] * 13, "sample", [None]):
            with self.subTest(steps=steps), self.assertRaises(HttpError) as invalid:
                publish_procedure_version(self.manager, self.procedure.pk, 0, steps)
            self.assertEqual(invalid.exception.status_code, 422)
        self.assertFalse(Version.objects.exists())
        self.assertFalse(Step.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_audit_failure_rolls_back_version_and_steps(self):
        def fail_audit(**fields):
            self.assertEqual(list(fields["version"].steps.values_list("instruction", flat=True)), ["Rinse."])
            raise IntegrityError("Simulated audit write failure")

        with patch.object(AuditEvent.objects, "create", side_effect=fail_audit):
            with self.assertRaisesMessage(IntegrityError, "Simulated audit write failure"):
                publish_procedure_version(self.manager, self.procedure.pk, 0, ["Rinse."])
        self.assertFalse(Version.objects.exists())
        self.assertFalse(Step.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_database_enforces_unique_revisions_and_step_positions(self):
        publish_procedure_version(self.manager, self.procedure.pk, 0, ["Rinse."])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Version.objects.create(procedure=self.procedure, revision=1)
        with transaction.atomic():
            v2 = Version.objects.create(procedure=self.procedure, revision=2)
            Step.objects.create(version=v2, position=1, instruction="First.")
            with self.assertRaises(IntegrityError), transaction.atomic():
                Step.objects.create(version=v2, position=1, instruction="Duplicate.")
            AuditEvent.objects.create(actor=self.manager, action=AuditEvent.Action.PUBLISHED, version=v2)

    def test_database_requires_a_publication_event_and_contiguous_steps(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Version.objects.create(procedure=self.procedure, revision=1)
            # TestCase wraps each test in a transaction. Force its commit-time check here.
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS required_publication IMMEDIATE")
        with self.assertRaises(IntegrityError), transaction.atomic():
            v1 = Version.objects.create(procedure=self.procedure, revision=1)
            Step.objects.create(version=v1, position=2, instruction="Missing step one.")
            AuditEvent.objects.create(actor=self.manager, action=AuditEvent.Action.PUBLISHED, version=v1)
        self.assertFalse(Version.objects.exists())

    def test_database_rejects_in_place_rewrites_and_late_inserts(self):
        v1 = publish_procedure_version(self.manager, self.procedure.pk, 0, ["Rinse."])
        old_order = self.issue_sample("HL-009", v1)
        v2 = publish_procedure_version(self.manager, self.procedure.pk, 1, ["Label."])
        with self.assertRaises(ProtectedError) as protected:
            v1.delete()
        self.assertIn(old_order, protected.exception.protected_objects)
        writes = {
            "revision": lambda: Version.objects.filter(pk=v1.pk).update(revision=3),
            "text": lambda: v1.steps.update(instruction="Changed."),
            "remove step": lambda: v1.steps.all().delete(),
            "append step": lambda: Step.objects.create(version=v1, position=2, instruction="Added."),
            "order binding": lambda: WorkOrder.objects.filter(pk=old_order.pk).update(version=v2),
            "issue old version": lambda: self.issue_sample("HL-014", v1),
            "procedure kind": lambda: Procedure.objects.filter(pk=self.procedure.pk).update(kind=Procedure.Kind.RECEIVE),
            "delete audit": lambda: AuditEvent.objects.filter(version=v1).delete(),
        }
        for name, write in writes.items():
            with self.subTest(write=name), self.assertRaises(IntegrityError), transaction.atomic():
                write()
        old_order.refresh_from_db()
        self.assertEqual(old_order.version_id, v1.pk)
        self.assertEqual(old_order.version.steps.get().instruction, "Rinse.")


class ConcurrentPublicationTests(TransactionTestCase):
    def test_two_managers_with_the_same_base_produce_one_revision_and_one_conflict(self):
        managers = [
            User.objects.create_user(username=name, role=User.Role.CellarManager)
            for name in ("manager-a", "manager-b")
        ]
        procedure = Procedure.objects.create(kind=Procedure.Kind.SAMPLE)
        publish_procedure_version(managers[0], procedure.pk, 0, ["Original."])
        ready = Barrier(2)

        def publish(manager):
            try:
                # Each worker has its own PostgreSQL session and transaction.
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                ready.wait(timeout=10)
                return publish_procedure_version(manager, procedure.pk, 1, [manager.username])
            finally:
                connections.close_all()

        winners, errors = [], []
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempts = [(manager, pool.submit(publish, manager)) for manager in managers]
            for manager, attempt in attempts:
                try:
                    winners.append((manager.pk, attempt.result(timeout=15).pk))
                except HttpError as exc:
                    errors.append(exc.status_code)

        self.assertEqual(errors, [409])
        revisions = procedure.versions.order_by("revision").values_list("revision", flat=True)
        self.assertEqual(list(revisions), [1, 2])
        winner = procedure.versions.get(revision=2)
        self.assertEqual(winners, [(winner.publication.actor_id, winner.pk)])
        self.assertEqual(winner.steps.get().instruction, winner.publication.actor.username)
        self.assertEqual(AuditEvent.objects.count(), 2)
