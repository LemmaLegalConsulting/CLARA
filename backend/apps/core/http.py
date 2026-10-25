import json
from functools import wraps

from django.conf import settings
from django.http import JsonResponse


class JsonBodyError(ValueError):
    """Raised when a request body is not the JSON object an endpoint expects."""


def json_body(request):
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonBodyError("Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise JsonBodyError("Request body must be a JSON object.")
    return payload


def method_not_allowed(methods):
    return JsonResponse({"error": f"Use one of: {', '.join(methods)}"}, status=405)


def json_body_errors_to_400(view_func):
    """Translate a malformed request body into a 400 instead of a 500."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except JsonBodyError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    return wrapped


def api_login_required(view_func):
    """Require an authenticated user and reject malformed JSON bodies with a 400."""
    guarded = json_body_errors_to_400(view_func)

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)
        from apps.core.constrained_worker import WorkerBusyError
        try:
            return guarded(request, *args, **kwargs)
        except WorkerBusyError as exc:
            response = JsonResponse({"error": str(exc)}, status=503)
            response["Retry-After"] = "5"
            return response

    return wrapped


def allow_document_framing(response):
    """Let this deployment's own app embed the document in a frame.

    The app previews PDFs -- a decision scan, a library source, a case
    document -- in an iframe, and in the split deployment it is served from a
    sibling subdomain rather than from the API. ``X-Frame-Options`` has no way
    to say "that origin": its only useful value here is SAMEORIGIN, which means
    the API's own origin, so the browser refused to display a PDF the app had
    already fetched successfully.

    ``frame-ancestors`` can name the app, so document responses send that and
    not the header it replaces. Browsers disagree about which one wins when
    both arrive, and the one that cannot express this deployment is the one
    that would win, so the opt-out below keeps the clickjacking middleware from
    adding it back on the way out.
    """
    ancestors = " ".join(["'self'", *getattr(settings, "FRAME_ANCESTORS", [])])
    response["Content-Security-Policy"] = f"frame-ancestors {ancestors}"
    response.xframe_options_exempt = True
    return response
