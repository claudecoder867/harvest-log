from django.contrib.auth.views import LogoutView
from django.urls import path

from cellar import views
from cellar.api import api

urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/login/", views.SignInView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("manager/", views.manager, name="manager"),
    path("manager/procedures/", views.procedure_editor, name="procedure_editor"),
    path("api/v1/", api.urls),
]
