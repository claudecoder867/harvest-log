import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from django.db.models import Prefetch, Q
from ninja import Field, NinjaAPI, Schema, Status
from ninja.errors import HttpError, ValidationError
from ninja.parser import Parser
from ninja.security import SessionAuth, django_auth
from pydantic import ConfigDict, StringConstraints

from . import services
from .models import (
    MAX_INSTRUCTION_LENGTH, MAX_STEPS, AuditEvent, LabReading, Version, WorkKind, WorkOrder,
)

class DecimalJSONParser(Parser):
    def parse_body(self, request):
        # Preserve the supplied precision until the measurement schema validates it.
        return json.loads(request.body, parse_float=Decimal)


api = NinjaAPI(
    title="Harvest Log", version="1.0.0", auth=django_auth, parser=DecimalJSONParser(),
    description="Sign in with a Django session. On POST, send the csrftoken cookie value "
                "in the X-CSRFToken header.",
)


class ManagerSession(SessionAuth):
    def authenticate(self, request, key):
        user = super().authenticate(request, key)
        if user is not None:
            services.require_manager(user)
        return user


manager_session = ManagerSession()
RecordId = Annotated[int, Field(gt=0, le=2**63 - 1)]
OrderCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40,
                                            pattern=r"^[^\x00]+$")]
Instruction = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                              max_length=MAX_INSTRUCTION_LENGTH)]


class Input(Schema):
    model_config = ConfigDict(extra="forbid")


class IssueIn(Input):
    kind: WorkKind
    lot_id: RecordId
    assignee_id: RecordId
    vessel_id: RecordId | None = None
    code: OrderCode | None = None


class TransitionIn(Input):
    action: Literal["start", "complete"]
    net_kg: Decimal | None = Field(None, gt=0, max_digits=12, decimal_places=3)
    elapsed_minutes: int | None = Field(None, strict=True, gt=0, le=2**31 - 1)


class LabIn(Input):
    analyte: str = Field(min_length=1, max_length=32)
    value: Decimal = Field(max_digits=12, decimal_places=4)


class PublishIn(Input):
    base_revision: int = Field(ge=0, strict=True)
    steps: list[Instruction] = Field(min_length=1, max_length=MAX_STEPS)


class StepOut(Schema):
    position: int
    instruction: str


class VersionOut(Schema):
    id: int
    revision: int
    published_by: int = Field(alias="publication.actor_id")
    published_at: datetime = Field(alias="publication.occurred_at")
    steps: list[StepOut]


class LabOut(Schema):
    id: int
    analyte: str = Field(alias="analyte_id")
    value: Decimal
    unit: str = Field(alias="analyte.unit")
    recorded_by: int = Field(alias="creation_event.actor_id")
    recorded_at: datetime = Field(alias="creation_event.occurred_at")


class EventOut(Schema):
    id: int
    action: AuditEvent.Action
    actor_id: int
    occurred_at: datetime
    changes: dict
    lab_reading_id: int | None


class OrderOut(Schema):
    id: int
    code: str
    kind: WorkKind
    lot_id: int
    vessel_id: int | None
    assignee_id: int
    status: WorkOrder.Status
    version_id: int
    revision: int = Field(alias="version.revision")


class OrderDetail(OrderOut):
    version: VersionOut
    net_kg: Decimal | None = Field(None, alias="receipt.net_kg")
    elapsed_minutes: int | None = Field(None, alias="pump_over.elapsed_minutes")
    lab_readings: list[LabOut]
    timeline: list[EventOut]

    @staticmethod
    def resolve_timeline(order):
        return AuditEvent.objects.filter(
            Q(work_order=order) | Q(lab_reading__work_order=order)
        ).order_by("occurred_at", "pk")


class ErrorOut(Schema):
    code: str
    message: str
    field: str | None = None


ERROR_CODES = {
    401: "authentication_required", 403: "forbidden", 404: "not_found",
    409: "conflict", 422: "invalid_input",
}


@api.exception_handler(HttpError)
def http_error(request, exc):
    status = exc.status_code
    message = str(exc)
    field = getattr(exc, "field", None)
    if status == 400:  # Ninja emits 400 for a malformed JSON body.
        status, message, field = 422, "Enter a valid JSON body.", "body"
    return api.create_response(request, {
        "code": ERROR_CODES.get(status, "request_error"), "message": message, "field": field,
    }, status=status)


@api.exception_handler(ValidationError)
def invalid_input(request, exc):
    error = exc.errors[0]
    location = list(error["loc"])
    if location[:2] == ["body", "payload"]:
        location = location[2:] or ["body"]
    field = ".".join(map(str, location))
    return api.create_response(request, {
        "code": "invalid_input", "message": error["msg"], "field": field,
    }, status=422)


def responses(status, schema, *errors):
    return {status: schema, **dict.fromkeys((401, 403, *errors), ErrorOut)}


@api.get("/work-orders", response=responses(200, list[OrderOut]))
def list_work_orders(request):
    """Managers see every order. Cellar hands see orders assigned to them."""
    return services.permitted_orders(request.user).order_by("pk")


@api.get("/work-orders/{id}", response=responses(200, OrderDetail, 404, 422))
def get_work_order(request, id: RecordId):
    """Open a permitted order with its pinned instructions, measurements and timeline."""
    readings = LabReading.objects.select_related("analyte", "creation_event").order_by("pk")
    orders = services.permitted_orders(request.user).select_related(
        "version__publication", "receipt", "pump_over",
    ).prefetch_related("version__steps", Prefetch("lab_readings", queryset=readings))
    try:
        return orders.get(pk=id)
    except WorkOrder.DoesNotExist as exc:
        raise HttpError(404, "Work order not found.") from exc


@api.post("/work-orders", auth=manager_session, response=responses(201, OrderOut, 409, 422))
def issue_work_order(request, payload: IssueIn):
    """Managers issue an order under the current published revision of its kind."""
    return Status(201, services.issue_work_order(request.user, **payload.model_dump()))


@api.post("/work-orders/{id}/transitions", response=responses(200, OrderOut, 404, 409, 422))
def transition_work_order(request, id: RecordId, payload: TransitionIn):
    """Managers or the assignee can start or complete work. Supply kg or minutes on completion."""
    if payload.action == "start":
        for field in ("net_kg", "elapsed_minutes"):
            if getattr(payload, field) is not None:
                raise services.InputError(field, "Supply this measurement on completion.")
        return services.start_work_order(request.user, id)
    return services.complete_work_order(
        request.user, id, net_kg=payload.net_kg, elapsed_minutes=payload.elapsed_minutes,
    )


@api.post("/work-orders/{id}/lab-results", response=responses(201, LabOut, 404, 409, 422))
def record_lab_result(request, id: RecordId, payload: LabIn):
    """Managers or the assignee can record a result on an in-progress sample."""
    reading = services.record_lab_reading(request.user, id, payload.analyte, payload.value)
    return Status(201, reading)


@api.get("/procedures/{kind}/versions", auth=manager_session, response=responses(200, list[VersionOut], 422))
def list_versions(request, kind: WorkKind):
    """Managers can read published versions of this procedure, newest first."""
    return Version.objects.filter(procedure__kind=kind, publication__isnull=False).select_related(
        "publication",
    ).prefetch_related("steps").order_by("-revision")


@api.post("/procedures/{kind}/versions", auth=manager_session, response=responses(201, VersionOut, 409, 422))
def publish_version(request, kind: WorkKind, payload: PublishIn):
    """Managers publish ordered steps against a base revision. Use zero for the first revision."""
    version = services.publish_version_for_kind(request.user, kind, payload.base_revision, payload.steps)
    return Status(201, version)
