from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Now


class CellarRole(models.TextChoices):
    CellarHand = "hand", "Cellar hand"
    CellarManager = "manager", "Cellar manager"


class User(AbstractUser):
    Role = CellarRole
    role = models.CharField(max_length=7, choices=CellarRole.choices)
    REQUIRED_FIELDS = [*AbstractUser.REQUIRED_FIELDS, "role"]

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=CellarRole.values),
                name="cellar_user_role_valid",
            ),
        ]


class Season(models.Model):
    year = models.PositiveSmallIntegerField(unique=True)


class Lot(models.Model):
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="lots")
    code = models.CharField(max_length=40)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["season", "code"], name="cellar_lot_season_code_uniq"),
        ]


class Vessel(models.Model):
    code = models.CharField(max_length=40, unique=True)


class WorkKind(models.TextChoices):
    RECEIVE = "receive", "Receive a load"
    PUMP_OVER = "pump_over", "Pump over"
    SAMPLE = "sample", "Take a lab sample"


class Procedure(models.Model):
    Kind = WorkKind
    kind = models.CharField(max_length=12, choices=WorkKind.choices, unique=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=WorkKind.values), name="cellar_kind_valid",
            ),
        ]


class Version(models.Model):
    procedure = models.ForeignKey(Procedure, on_delete=models.PROTECT, related_name="versions")
    revision = models.PositiveIntegerField()

    class Meta:
        db_table = "cellar_procedureversion"
        constraints = [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="cellar_revision_positive"),
            models.UniqueConstraint(fields=["procedure", "revision"], name="cellar_proc_revision_uniq"),
        ]


MAX_STEPS = 12
MAX_INSTRUCTION_LENGTH = 240


class Step(models.Model):
    version = models.ForeignKey(Version, on_delete=models.PROTECT, related_name="steps")
    position = models.PositiveSmallIntegerField()
    instruction = models.CharField(max_length=MAX_INSTRUCTION_LENGTH)

    class Meta:
        db_table = "cellar_procedurestep"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["version", "position"], name="cellar_step_position_uniq"),
            models.CheckConstraint(
                condition=models.Q(position__range=(1, MAX_STEPS)), name="cellar_step_range",
            ),
            models.CheckConstraint(
                condition=models.Q(instruction__regex=r"^[^[:space:]](.*[^[:space:]])?$"),
                name="cellar_step_text_trimmed",
            ),
        ]


class OrderStatus(models.TextChoices):
    NOT_STARTED = "not_started", "Not started"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"


class WorkOrder(models.Model):
    Status = OrderStatus
    code = models.CharField(max_length=40, unique=True)
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="work_orders")
    vessel = models.ForeignKey(
        Vessel, on_delete=models.PROTECT, related_name="work_orders", null=True, blank=True,
    )
    assignee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="assigned_orders")
    version = models.ForeignKey(Version, on_delete=models.PROTECT, related_name="work_orders")
    status = models.CharField(
        max_length=12, choices=OrderStatus.choices, default=OrderStatus.NOT_STARTED,
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=OrderStatus.values), name="cellar_status_valid",
            ),
        ]

    @property
    def kind(self):
        return self.version.procedure.kind


class Receipt(models.Model):
    work_order = models.OneToOneField(WorkOrder, on_delete=models.PROTECT, related_name="receipt")
    net_kg = models.DecimalField(max_digits=12, decimal_places=3)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(net_kg__gt=0), name="cellar_net_kg_positive"),
        ]


class PumpOver(models.Model):
    work_order = models.OneToOneField(WorkOrder, on_delete=models.PROTECT, related_name="pump_over")
    elapsed_minutes = models.PositiveIntegerField()

    class Meta:
        db_table = "cellar_pumpoverdetail"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(elapsed_minutes__gt=0), name="cellar_elapsed_minutes_positive",
            ),
        ]


class Analyte(models.Model):
    code = models.SlugField(primary_key=True, max_length=32)
    name = models.CharField(max_length=80)
    unit = models.CharField(max_length=16, blank=True)


class LabReading(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="lab_readings")
    analyte = models.ForeignKey(Analyte, on_delete=models.PROTECT, related_name="readings")
    value = models.DecimalField(max_digits=12, decimal_places=4)


class AuditEvent(models.Model):
    class Action(models.TextChoices):
        PUBLISHED = "version_published", "Published a procedure version"
        COMPLETED = "work_order_completed", "Completed a work order"
        LAB_RECORDED = "lab_reading_recorded", "Recorded a lab reading"

    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="audit_events")
    occurred_at = models.DateTimeField(db_default=Now(), editable=False)
    action = models.CharField(max_length=32, choices=Action.choices)
    version = models.OneToOneField(
        Version, null=True, blank=True, on_delete=models.PROTECT, related_name="publication",
    )
    work_order = models.ForeignKey(
        WorkOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events",
    )
    lab_reading = models.OneToOneField(
        LabReading, null=True, blank=True, on_delete=models.PROTECT, related_name="creation_event",
    )
    changes = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(action="version_published", version__isnull=False,
                             work_order__isnull=True, lab_reading__isnull=True)
                    | models.Q(action="work_order_completed", work_order__isnull=False,
                               version__isnull=True, lab_reading__isnull=True)
                    | models.Q(action="lab_reading_recorded", lab_reading__isnull=False,
                               version__isnull=True, work_order__isnull=True)
                ),
                name="cellar_event_matches_target",
            ),
            models.UniqueConstraint(
                fields=["work_order"], condition=models.Q(action="work_order_completed"),
                name="cellar_one_completion",
            ),
        ]
