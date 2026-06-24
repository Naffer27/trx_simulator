"""
simulator/permissions.py

Lightweight permission helpers for Django admin actions and views.
No dependency on models, admin, or views.
"""
import functools

from django.contrib import messages as _msgs


def superuser_required_action(fn):
    """
    Decorator for Django admin action functions and ModelAdmin methods.

    Blocks execution and shows an error message for non-superuser staff.
    Works transparently for both module-level actions (modeladmin, request, queryset)
    and instance method actions (self, request, queryset).
    Preserves all Django admin attributes (short_description, etc.) via functools.wraps.
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        # args[0] = modeladmin or self, args[1] = request, args[2] = queryset
        modeladmin, request = args[0], args[1]
        if not request.user.is_superuser:
            modeladmin.message_user(
                request,
                "Permiso denegado — esta acción requiere superusuario.",
                _msgs.ERROR,
            )
            return
        return fn(*args, **kwargs)
    return _wrapped
