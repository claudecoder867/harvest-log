from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from .models import CellarRole


class SignInView(LoginView):
    template_name = "cellar/login.html"


@login_required
@require_safe
def home(request):
    if request.user.role == CellarRole.CellarManager:
        return redirect("manager")
    return render(request, "cellar/workspace.html", {"heading": "My cellar"})


@login_required
@require_safe
def manager(request):
    if request.user.role != CellarRole.CellarManager:
        raise PermissionDenied
    return render(request, "cellar/workspace.html", {"heading": "Manager workspace"})
