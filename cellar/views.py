from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db.models import Case, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from .models import Analyte, CellarRole, Lot, Season, User, Version, Vessel, WorkKind, WorkOrder
from .reports import season_totals
from .services import order_timeline, orders_with_details, permitted_orders


class SignInView(LoginView):
    template_name = "cellar/login.html"


def _render_work_queue(request):
    priority = Case(
        When(status=WorkOrder.Status.IN_PROGRESS, then=0),
        When(status=WorkOrder.Status.NOT_STARTED, then=1),
        default=2,
    )
    orders = (
        permitted_orders(request.user)
        .select_related("lot", "vessel", "assignee")
        .prefetch_related("version__steps")
        .order_by(priority, "pk")
    )
    context = {"work_orders": orders}
    if request.user.role == CellarRole.CellarManager:
        context.update(
            lots=Lot.objects.select_related("season").order_by("-season__year", "code"),
            vessels=Vessel.objects.order_by("code"),
            assignees=User.objects.filter(is_active=True, role__in=CellarRole.values).order_by("username"),
        )
    return render(request, "cellar/work_queue.html", context)


@login_required
@require_safe
def home(request):
    if request.user.role == CellarRole.CellarManager:
        return redirect("manager")
    return _render_work_queue(request)


@login_required
@require_safe
def work_order_detail(request, pk):
    order = get_object_or_404(
        orders_with_details(request.user).select_related("lot", "vessel", "assignee"), pk=pk,
    )
    context = {
        "order": order,
        "timeline": order_timeline(order).select_related("actor", "lab_reading__analyte"),
    }
    if order.kind == WorkKind.SAMPLE and order.status == WorkOrder.Status.IN_PROGRESS:
        context["analytes"] = Analyte.objects.order_by("name")
    return render(request, "cellar/work_order_detail.html", context)


@login_required
@require_safe
def manager(request):
    if request.user.role != CellarRole.CellarManager:
        raise PermissionDenied
    return _render_work_queue(request)


@login_required
@require_safe
def procedure_editor(request):
    if request.user.role != CellarRole.CellarManager:
        raise PermissionDenied
    kind = request.GET.get("kind", WorkKind.RECEIVE)
    if kind not in WorkKind.values:
        raise Http404("Unknown procedure type.")
    published_version = (
        Version.objects.filter(procedure__kind=kind, publication__isnull=False)
        .prefetch_related("steps")
        .order_by("-revision")
        .first()
    )
    return render(request, "cellar/procedure_editor.html", {
        "kind": kind, "published_version": published_version,
    })


@login_required
@require_safe
def season_report(request, year=None):
    if request.user.role != CellarRole.CellarManager:
        raise PermissionDenied
    years = list(Season.objects.order_by("-year").values_list("year", flat=True))
    if year is None:
        year = years[0] if years else timezone.localdate().year
    if not 1 <= year <= 9999:
        raise Http404("Invalid season year.")
    return render(request, "cellar/season_report.html", {"report": season_totals(year), "years": years})
