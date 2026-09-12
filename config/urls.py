from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path(
        "admin/",
        admin.site.urls,
    ),

    # Built-in login, logout, password change, and password reset routes.
    path(
        "accounts/",
        include("django.contrib.auth.urls"),
    ),

    # Supply-chain strategy simulation.
    path(
        "",
        include("simulation.urls"),
    ),
]
