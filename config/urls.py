from django.contrib.auth.views import LogoutView
from django.urls import path

from cellar import views
from cellar.api import api

urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/login/", views.SignInView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("work-orders/<int:pk>/", views.work_order_detail, name="work_order_detail"),
    path("manager/", views.manager, name="manager"),
    path("manager/procedures/", views.procedure_editor, name="procedure_editor"),
    path("manager/reports/seasons/", views.season_report, name="season_report_latest"),
    path("manager/reports/seasons/<int:year>/", views.season_report, name="season_report"),
    path("api/v1/", api.urls),
]
