from django.contrib.auth.models import AbstractUser
from django.db import models


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
