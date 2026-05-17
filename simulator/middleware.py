"""
simulator/middleware.py
RequestIDMiddleware — stamps every request with X-Request-ID for log correlation.
"""
import uuid
from .observability import set_request_id


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        set_request_id(rid)
        request.request_id = rid
        response = self.get_response(request)
        response["X-Request-ID"] = rid
        return response
