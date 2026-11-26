from django.core.exceptions import ValidationError
from django.db import transaction
from ninja.errors import HttpError

from .models import (
    MAX_INSTRUCTION_LENGTH, MAX_STEPS, Analyte, AuditEvent, CellarRole, LabReading,
    Procedure, PumpOver, Receipt, Step, Version, WorkKind, WorkOrder,
)


def _clean_steps(new_steps):
    if not isinstance(new_steps, (list, tuple)) or not 1 <= len(new_steps) <= MAX_STEPS:
        raise HttpError(422, f"Provide between 1 and {MAX_STEPS} ordered steps.")

    instructions = []
    for position, value in enumerate(new_steps, 1):
        instruction = value.strip() if isinstance(value, str) else ""
        if not 1 <= len(instruction) <= MAX_INSTRUCTION_LENGTH or "\x00" in instruction:
            raise HttpError(
                422, f"Step {position} must contain 1–{MAX_INSTRUCTION_LENGTH} characters of text.",
            )
        instructions.append(instruction)
    return instructions


def publish_procedure_version(manager_user, procedure_id, base_revision, new_steps):
    """Publish a complete snapshot. Use base_revision=0 for a procedure's first publication."""
    if manager_user is None or not manager_user.is_authenticated:
        raise HttpError(401, "Sign in to publish a procedure.")
    if not manager_user.is_active or manager_user.role != CellarRole.CellarManager:
        raise HttpError(403, "Only a cellar manager can publish a procedure.")
    if type(base_revision) is not int or base_revision < 0:
        raise HttpError(422, "Base revision must be a non-negative integer.")

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


def _lock_in_progress_order(user, work_order_id):
    if user is None or not user.is_authenticated:
        raise HttpError(401, "Sign in to update a work order.")
    if not user.is_active or user.role not in CellarRole.values:
        raise HttpError(403, "You do not have permission to update work orders.")

    orders = WorkOrder.objects.select_related("version__procedure").select_for_update(of=("self",))
    if user.role == CellarRole.CellarHand:
        orders = orders.filter(assignee=user)
    try:
        order = orders.get(pk=work_order_id)
    except WorkOrder.DoesNotExist as exc:
        raise HttpError(404, "Work order not found.") from exc
    if order.status != WorkOrder.Status.IN_PROGRESS:
        raise HttpError(409, "This operation requires an in-progress work order.")
    return order


def complete_work_order(user, work_order_id):
    with transaction.atomic():
        order = _lock_in_progress_order(user, work_order_id)
        if order.kind == WorkKind.SAMPLE and not order.lab_readings.exists():
            raise HttpError(409, "Record a lab reading before completing this sample order.")
        if order.kind == WorkKind.RECEIVE and not Receipt.objects.filter(work_order=order).exists():
            raise HttpError(409, "Record the net kg before completing this receiving order.")
        if order.kind == WorkKind.PUMP_OVER and not PumpOver.objects.filter(work_order=order).exists():
            raise HttpError(409, "Record the elapsed minutes before completing this pump-over.")

        previous_status = order.status
        order.status = WorkOrder.Status.COMPLETED
        order.save(update_fields=["status"])
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.COMPLETED, work_order=order,
            changes={"status": {"from": previous_status, "to": order.status}},
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
            raise HttpError(422, "Select a registered analyte.") from exc
        try:
            value = LabReading._meta.get_field("value").clean(str(value), None)
        except ValidationError as exc:
            raise HttpError(422, "Enter a finite decimal within the reading field's precision.") from exc

        reading = LabReading.objects.create(work_order=order, analyte=registered, value=value)
        AuditEvent.objects.create(
            actor=user, action=AuditEvent.Action.LAB_RECORDED, lab_reading=reading,
            changes={"analyte": registered.pk, "value": format(value, "f")},
        )
    return reading
