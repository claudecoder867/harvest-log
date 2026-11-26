from django.contrib.auth.views import LogoutView
from django.urls import path
from ninja import NinjaAPI
from ninja.security import django_auth

from cellar import views

api = NinjaAPI(title="Harvest Log", version="1.0.0", auth=django_auth)

urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/login/", views.SignInView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("manager/", views.manager, name="manager"),
    path("api/v1/", api.urls),
]
