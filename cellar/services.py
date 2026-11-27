from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from ninja.errors import HttpError

from .models import (
    MAX_INSTRUCTION_LENGTH, MAX_STEPS, Analyte, AuditEvent, CellarRole, LabReading,
    Lot, Procedure, PumpOver, Receipt, Step, User, Version, Vessel, WorkKind, WorkOrder,
)


class InputError(HttpError):
    def __init__(self, field, message):
        super().__init__(422, message)
        self.field = field


def require_cellar_user(user):
    if user is None or not user.is_authenticated:
        raise HttpError(401, "Sign in to access Harvest Log.")
    if not user.is_active or user.role not in CellarRole.values:
        raise HttpError(403, "An active cellar role is required.")


def require_manager(user):
    require_cellar_user(user)
    if user.role != CellarRole.CellarManager:
        raise HttpError(403, "Only a cellar manager can perform this action.")


def _clean_steps(new_steps):
    if not isinstance(new_steps, (list, tuple)) or not 1 <= len(new_steps) <= MAX_STEPS:
        raise InputError("steps", f"Provide between 1 and {MAX_STEPS} ordered steps.")

    instructions = []
    for position, value in enumerate(new_steps, 1):
        instruction = value.strip() if isinstance(value, str) else ""
        if not 1 <= len(instruction) <= MAX_INSTRUCTION_LENGTH or "\x00" in instruction:
            raise InputError(
                f"steps.{position - 1}", f"Step {position} must contain 1–{MAX_INSTRUCTION_LENGTH} characters of text.",
            )
        instructions.append(instruction)
    return instructions


def publish_procedure_version(manager_user, procedure_id, base_revision, new_steps):
    """Publish a complete snapshot. Use base_revision=0 for a procedure's first publication."""
    require_manager(manager_user)
    if type(base_revision) is not int or base_revision < 0:
        raise InputError("base_revision", "Base revision must be a non-negative integer.")

    with transaction.atomic():
        try:
            procedure = Procedure.objects.select_for_update().get(pk=procedure_id)
        except Procedure.DoesNotExist as exc:
            raise HttpError(404, "Procedure not found.") from exc

        latest = procedure.versions.order_by("-revision").values_list("revision", flat=True).first()
        current_revision = latest or 0
        if base_revision != current_revision:
            raise HttpError(
                409, f"Revision {current_revision} is current. Reload the procedure before publishing.",
            )

        instructions = _clean_steps(new_steps)
        version = Version.objects.create(procedure=procedure, revision=current_revision + 1)
        Step.objects.bulk_create([
            Step(version=version, position=position, instruction=instruction)
            for position, instruction in enumerate(instructions, 1)
        ])
        AuditEvent.objects.create(
            actor=manager_user, action=AuditEvent.Action.PUBLISHED, version=version,
        )
    return version


def publish_version_for_kind(user, kind, base_revision, steps):
    require_manager(user)
    with transaction.atomic():
        procedure, _ = Procedure.objects.get_or_create(kind=kind)
        return publish_procedure_version(user, procedure.pk, base_revision, steps)


def permitted_orders(user):
    require_cellar_user(user)
    orders = WorkOrder.objects.select_related("version__procedure")
    if user.role == CellarRole.CellarHand:
        orders = orders.filter(assignee=user)
    return orders


def orders_with_details(user):
    readings = LabReading.objects.select_related("analyte", "creation_event").order_by("pk")
    return (
        permitted_orders(user)
        .select_related("version__publication", "receipt", "pump_over")
        .prefetch_related("version__steps", Prefetch("lab_readings", queryset=readings))
    )


def order_timeline(order):
    return AuditEvent.objects.filter(
        Q(work_order=order) | Q(lab_reading__work_order=order)
    ).order_by("occurred_at", "pk")


def issue_work_order(user, *, kind, lot_id, assignee_id, vessel_id=None, code=None):
    require_manager(user)
    with transaction.atomic():
        procedure = Procedure.objects.select_for_update().filter(kind=kind).first()
        version = Version.objects.filter(
            procedure=procedure, publication__isnull=False,
        ).order_by("-revision").first()
        if version is None:
            raise HttpError(409, "Publish a procedure version before issuing this work.")
        if not Lot.objects.select_for_update(no_key=True).filter(pk=lot_id).exists():
            raise InputError("lot_id", "Select an existing lot.")
        if vessel_id is not None and not Vessel.objects.select_for_update(no_key=True).filter(pk=vessel_id).exists():
            raise InputError("vessel_id", "Select an existing vessel.")
        assignees = User.objects.select_for_update(no_key=True).filter(is_active=True, role__in=CellarRole.values)
        if not assignees.filter(pk=assignee_id).exists():
            raise InputError("assignee_id", "Select an active cellar user.")

        order, created = WorkOrder.objects.get_or_create(
            code=code if code is not None else f"HL-{uuid4().hex}",
            defaults=dict(lot_id=lot_id, vessel_id=vessel_id, assignee_id=assignee_id, version=version),
        )
        if not created:
            raise HttpError(409, "This work order code is already in use.")
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.ISSUED, work_order=order,
            changes={"version_id": version.pk, "lot_id": lot_id, "vessel_id": vessel_id,
                     "assignee_id": assignee_id, "status": order.status},
        )
    return order


def _lock_order(user, work_order_id):
    orders = permitted_orders(user).select_for_update(of=("self",))
    try:
        order = orders.get(pk=work_order_id)
    except WorkOrder.DoesNotExist as exc:
        raise HttpError(404, "Work order not found.") from exc
    return order


def start_work_order(user, work_order_id):
    with transaction.atomic():
        order = _lock_order(user, work_order_id)
        if order.status != WorkOrder.Status.NOT_STARTED:
            raise HttpError(409, "Only a work order that has not started can be started.")
        order.status = WorkOrder.Status.IN_PROGRESS
        order.save(update_fields=["status"])
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.STARTED, work_order=order,
            changes={"status": {"from": WorkOrder.Status.NOT_STARTED, "to": order.status}},
        )
    return order


def _lock_in_progress_order(user, work_order_id):
    order = _lock_order(user, work_order_id)
    if order.status != WorkOrder.Status.IN_PROGRESS:
        raise HttpError(409, "This operation requires an in-progress work order.")
    return order


def _completion_measurement(order, net_kg, elapsed_minutes):
    if net_kg is not None and order.kind != WorkKind.RECEIVE:
        raise InputError("net_kg", "Net kg can only be recorded on a receiving order.")
    if elapsed_minutes is not None and order.kind != WorkKind.PUMP_OVER:
        raise InputError("elapsed_minutes", "Elapsed minutes require a pump-over order.")

    if order.kind == WorkKind.SAMPLE:
        if not order.lab_readings.exists():
            raise HttpError(409, "Record a lab reading before completing this sample order.")
        return {}

    if order.kind == WorkKind.RECEIVE:
        model, field, quantity = Receipt, "net_kg", net_kg
    else:
        model, field, quantity = PumpOver, "elapsed_minutes", elapsed_minutes
    if quantity is None:
        if not model.objects.filter(work_order=order).exists():
            raise HttpError(409, f"Record {field.replace('_', ' ')} before completing this order.")
        return {}

    try:
        quantity = model._meta.get_field(field).clean(quantity, None)
    except ValidationError as exc:
        raise InputError(field, "Enter a valid positive measurement.") from exc
    if quantity <= 0:
        raise InputError(field, "The measurement must be positive.")
    measure, created = model.objects.get_or_create(work_order=order, defaults={field: quantity})
    if not created and getattr(measure, field) != quantity:
        raise HttpError(409, "A different measurement is already recorded.")
    if not created:
        return {}
    return {field: str(quantity) if field == "net_kg" else quantity}


def complete_work_order(user, work_order_id, *, net_kg=None, elapsed_minutes=None):
    with transaction.atomic():
        order = _lock_in_progress_order(user, work_order_id)
        changes = _completion_measurement(order, net_kg, elapsed_minutes)
        previous_status = order.status
        order.status = WorkOrder.Status.COMPLETED
        order.save(update_fields=["status"])
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.COMPLETED, work_order=order,
            changes={**changes, "status": {"from": previous_status, "to": order.status}},
        )
    return order


def record_lab_reading(user, work_order_id, analyte, value):
    """Record a result using a registered Analyte or its code (e.g. 'ph' or 'brix')."""
    with transaction.atomic():
        order = _lock_in_progress_order(user, work_order_id)
        if order.kind != WorkKind.SAMPLE:
            raise HttpError(409, "Lab readings can only be recorded on sample orders.")

        code = analyte.pk if isinstance(analyte, Analyte) else analyte
        try:
            code = Analyte._meta.get_field("code").clean(code, None)
            registered = Analyte.objects.get(pk=code)
        except (ValidationError, Analyte.DoesNotExist) as exc:
            raise InputError("analyte", "Select a registered analyte.") from exc
        try:
            value = LabReading._meta.get_field("value").clean(str(value), None)
        except ValidationError as exc:
            raise InputError("value", "Enter a finite decimal within the reading field's precision.") from exc

        reading = LabReading.objects.create(work_order=order, analyte=registered, value=value)
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.LAB_RECORDED, lab_reading=reading,
            changes={"analyte": registered.pk, "value": format(value, "f")},
        )
    return reading
