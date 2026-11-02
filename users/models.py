import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # User identifiers are UUIDs so they are not guessable and do not leak
    # account counts. Migration 0002 converts the pre-existing integer
    # auth_user.id values (and every user_id foreign key) to UUIDs in place,
    # preserving existing accounts.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        db_table = 'auth_user'
