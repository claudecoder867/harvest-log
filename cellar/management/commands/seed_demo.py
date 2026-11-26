import os
from dataclasses import dataclass

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cellar import services
from cellar.models import CellarRole, Lot, Procedure, Season, User, Vessel, WorkKind, WorkOrder


STEPS = {
    WorkKind.RECEIVE: ["Confirm the lot on the delivery ticket.", "Record the net fruit weight in kg."],
    WorkKind.PUMP_OVER: ["Confirm the lot and tank.", "Record the elapsed pump-over time in minutes."],
    WorkKind.SAMPLE: ["Confirm the lot and tank.", "Collect the sample and record the lab result."],
}


@dataclass(frozen=True)
class DemoOrder:
    code: str
    kind: WorkKind
    status: WorkOrder.Status
    net_kg: int | None = None
    elapsed_minutes: int | None = None
    readings: tuple[tuple[str, str], ...] = ()
    year: int = 2026


Status = WorkOrder.Status
ORDERS = (
    DemoOrder("HL-001", WorkKind.RECEIVE, Status.COMPLETED, net_kg=1950),
    DemoOrder("HL-002", WorkKind.RECEIVE, Status.COMPLETED, net_kg=2100),
    DemoOrder("HL-003", WorkKind.RECEIVE, Status.COMPLETED, net_kg=2350),
    DemoOrder("HL-004", WorkKind.RECEIVE, Status.NOT_STARTED),
    DemoOrder("HL-005", WorkKind.PUMP_OVER, Status.COMPLETED, elapsed_minutes=15),
    DemoOrder("HL-006", WorkKind.PUMP_OVER, Status.COMPLETED, elapsed_minutes=20),
    DemoOrder("HL-007", WorkKind.PUMP_OVER, Status.COMPLETED, elapsed_minutes=18),
    DemoOrder("HL-008", WorkKind.PUMP_OVER, Status.IN_PROGRESS),
    DemoOrder("HL-009", WorkKind.SAMPLE, Status.IN_PROGRESS),
    DemoOrder("HL-010", WorkKind.SAMPLE, Status.COMPLETED, readings=(("ph", "3.41"), ("brix", "22.1"))),
    DemoOrder("HL-011", WorkKind.SAMPLE, Status.COMPLETED, readings=(("ph", "3.56"), ("brix", "21.6"))),
    DemoOrder("HL-012", WorkKind.SAMPLE, Status.NOT_STARTED),
    DemoOrder("HL-2025-001", WorkKind.RECEIVE, Status.COMPLETED, net_kg=900, year=2025),
)


def demo_user(role):
    username = f"demo_{role.value}"
    password_var = f"DEMO_{role.value.upper()}_PASSWORD"
    user, _ = User.objects.get_or_create(
        username=username,
        defaults={"role": role, "password": lambda: make_password(os.environ.get(password_var) or None)},
    )
    if user.role != role or not user.is_active:
        raise CommandError(f"{username} must be an active {role.label.lower()}.")
    return user


class Command(BaseCommand):
    help = (
        "Create the 2025 and 2026 demo fixtures. To enable password sign-in on first creation, "
        "set DEMO_MANAGER_PASSWORD and DEMO_HAND_PASSWORD."
    )

    def handle(self, *args, **options):
        created = 0
        with transaction.atomic():
            # Both seed runs acquire the same season lock before checking any order codes.
            lots = {}
            for year in (2025, 2026):
                season, _ = Season.objects.select_for_update().get_or_create(year=year)
                lot, _ = Lot.objects.get_or_create(season=season, code=f"DEMO-PN-{year}")
                lots[year] = lot
            manager = demo_user(CellarRole.CellarManager)
            hand = demo_user(CellarRole.CellarHand)
            tank, _ = Vessel.objects.get_or_create(code="T-04")
            for kind, steps in STEPS.items():
                procedure, _ = Procedure.objects.select_for_update().get_or_create(kind=kind)
                if not procedure.versions.exists():
                    services.publish_procedure_version(manager, procedure.pk, 0, steps)

            for spec in ORDERS:
                if WorkOrder.objects.filter(code=spec.code).exists():
                    continue
                order = services.issue_work_order(
                    manager, code=spec.code, kind=spec.kind, lot_id=lots[spec.year].pk,
                    assignee_id=hand.pk, vessel_id=None if spec.kind == WorkKind.RECEIVE else tank.pk,
                )
                created += 1
                if spec.status == Status.NOT_STARTED:
                    continue
                services.start_work_order(hand, order.pk)
                for analyte, value in spec.readings:
                    services.record_lab_reading(hand, order.pk, analyte, value)
                if spec.status == Status.COMPLETED:
                    services.complete_work_order(
                        hand, order.pk, net_kg=spec.net_kg, elapsed_minutes=spec.elapsed_minutes,
                    )
        self.stdout.write(self.style.SUCCESS(f"Created {created} demo work orders."))
