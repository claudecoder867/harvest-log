from decimal import Decimal

from django.db.models import Count, OuterRef, Q, Subquery, Sum

from .models import LabReading, Receipt, WorkKind, WorkOrder


def season_totals(year):
    # Each subquery groups its own measurement table. The outer query only counts orders.
    kind = "version__procedure__kind"
    order_kind = f"work_order__{kind}"
    scope = {"work_order__lot__season__year": year, order_kind: OuterRef(kind)}
    weights = Receipt.objects.filter(
        **scope, work_order__status=WorkOrder.Status.COMPLETED,
    ).order_by().values(order_kind).annotate(kg=Sum("net_kg"))
    readings = LabReading.objects.filter(**scope).order_by().values(order_kind).annotate(n=Count("pk"))

    groups = WorkOrder.objects.filter(lot__season__year=year).values(kind).annotate(
        total=Count("pk"),
        completed=Count("pk", filter=Q(status=WorkOrder.Status.COMPLETED)),
        in_progress=Count("pk", filter=Q(status=WorkOrder.Status.IN_PROGRESS)),
        received_kg=Subquery(weights.values("kg")),
        readings=Subquery(readings.values("n")),
    )
    by_kind = {row[kind]: row for row in groups}
    breakdown = []
    for work_kind in WorkKind:
        row = by_kind.get(work_kind, {})
        total = row.get("total", 0)
        completed = row.get("completed", 0)
        in_progress = row.get("in_progress", 0)
        breakdown.append({
            "kind": work_kind.value, "label": work_kind.label, "total": total,
            "completed": completed, "in_progress": in_progress,
            "not_started": total - completed - in_progress,
        })

    net_kg = sum((row["received_kg"] or 0 for row in by_kind.values()), Decimal(0))
    return {
        "year": year, "total_orders": sum(row["total"] for row in breakdown),
        "not_started_orders": sum(row["not_started"] for row in breakdown),
        "in_progress_orders": sum(row["in_progress"] for row in breakdown),
        "completed_orders": sum(row["completed"] for row in breakdown),
        "lab_readings": sum(row["readings"] or 0 for row in by_kind.values()),
        "net_tonnes_received": net_kg / Decimal(1000), "by_kind": breakdown,
    }
