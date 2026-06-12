"""
simulator/context_processors.py
"""


def readiness_context(request):
    """Inject readiness dict for authenticated users on every request."""
    if not (hasattr(request, "user") and request.user.is_authenticated):
        return {}
    from .readiness import get_user_readiness
    return {"readiness": get_user_readiness(request.user)}
