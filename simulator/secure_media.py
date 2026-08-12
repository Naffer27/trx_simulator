"""
O.5e-1 — Secure Media Serving Foundation.

RC-02 (Final Production Readiness Review) found that KYC documents,
Treasury evidence, and Broker documents have no safe way to be served in
production: Django's own `static()` helper (wired in trx_simulator/urls.py)
only adds a `/media/` route when DEBUG=True, and no Nginx `location
/media/` exists either — so under DEBUG=False staff cannot view these
files at all, and the moment anyone "fixes" that by adding a public
`/media/` location (in Nginx or otherwise), every file becomes reachable
by URL guessing with zero authorization (KYC selfies/documents included).

This module is the single choke point instead: three Django views, each
resolving its file from an authorized model instance (never from a
filesystem path or filename supplied by the caller), streaming bytes only
after an object-based permission check. No route under MEDIA_URL is
registered in production; every consumer (templates, Django admin) is
updated to link here instead of to `<field>.url`.

Authorization matrix (frozen by the O.5e-1 approval message):
  * KYCProfile.document_front / document_back / selfie
      -> the owning user (kyc.user), or staff holding
         view_kycprofile/change_kycprofile (mirrors KYCProfileAdmin's
         own default has_view_permission).
  * TreasuryOperationRequest.evidence
      -> staff holding any of the three existing Treasury permissions
         (submit/review/execute — treasury_requests.py), the same set
         TreasuryOperationRequestAdmin.has_view_permission already
         accepts. The O.4a TOTP-for-Treasury gate is normally enforced
         only inside MoneyBrokerAdminSite.admin_view() (admin.py) — since
         this view lives outside /admin/, it is replicated here so moving
         evidence access off the admin site does not quietly weaken it.
  * BrokerDocument.file
      -> any authenticated user when `public=True` (matches
         documents_view's existing `filter(public=True)`); staff holding
         view_brokerdocument/change_brokerdocument when `public=False`.

Every view returns Http404 (never 403) on any authorization failure or
missing object — deliberate choice so an authenticated user probing IDs
cannot distinguish "exists but not yours" from "does not exist" (IDOR
hardening). Unauthenticated requests are handled by @login_required
(redirect to login), which happens before any object lookup either way.
"""
import mimetypes
import os

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse

from .models import BrokerDocument, KYCProfile, TreasuryOperationRequest

_KYC_FILE_FIELDS = ("document_front", "document_back", "selfie")


def _safe_field_file_path(field_file):
    """
    Resolve a FieldFile to a real filesystem path, verifying it is
    actually contained inside MEDIA_ROOT and points at a real file.

    Independent, second-layer check on top of FileSystemStorage's own
    safe_join() (which already blocks '..' segments in the stored name):
    this also catches a symlink planted inside MEDIA_ROOT that resolves
    outside it. Raises Http404 (never a raw exception) for every failure
    mode — missing file, storage error, traversal, symlink escape — so a
    caller never learns which case it was.
    """
    if not field_file:
        raise Http404("File not found.")

    try:
        file_path = field_file.path
    except (NotImplementedError, ValueError, SuspiciousFileOperation):
        raise Http404("File not found.")

    media_root = os.path.realpath(str(settings.MEDIA_ROOT))
    real_path = os.path.realpath(file_path)
    try:
        contained = os.path.commonpath([media_root, real_path]) == media_root
    except ValueError:
        # commonpath() raises ValueError when paths are on different
        # drives (Windows) or otherwise not comparable — treat as escape.
        contained = False

    if not contained or not os.path.isfile(real_path):
        raise Http404("File not found.")

    return real_path


def _stream_field_file(field_file):
    """
    Stream a FieldFile's bytes as a FileResponse. Never loads the whole
    file into memory (FileResponse reads it in chunks); Content-Type,
    Content-Length and a safely-encoded Content-Disposition are computed
    by Django's own FileResponse.set_headers(), not hand-built here, so
    filenames containing quotes/unicode/spaces cannot break the header.
    """
    real_path = _safe_field_file_path(field_file)
    content_type, _ = mimetypes.guess_type(real_path)
    filename = os.path.basename(real_path)
    response = FileResponse(
        open(real_path, "rb"),
        as_attachment=False,
        filename=filename,
        content_type=content_type or "application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _kyc_staff_authorized(user):
    return user.is_staff and (
        user.has_perm("simulator.view_kycprofile")
        or user.has_perm("simulator.change_kycprofile")
    )


@login_required
def secure_kyc_media_view(request, kyc_id, field):
    if field not in _KYC_FILE_FIELDS:
        raise Http404("File not found.")

    kyc = get_object_or_404(KYCProfile, pk=kyc_id)

    is_owner = kyc.user_id == request.user.id
    if not (is_owner or _kyc_staff_authorized(request.user)):
        raise Http404("File not found.")

    return _stream_field_file(getattr(kyc, field))


def _treasury_staff_authorized(user):
    from .treasury_requests import (
        TREASURY_EXECUTE_PERMISSION,
        TREASURY_REVIEW_PERMISSION,
        TREASURY_SUBMIT_PERMISSION,
    )
    return user.is_staff and (
        user.has_perm(TREASURY_SUBMIT_PERMISSION)
        or user.has_perm(TREASURY_REVIEW_PERMISSION)
        or user.has_perm(TREASURY_EXECUTE_PERMISSION)
    )


@login_required
def secure_treasury_evidence_view(request, pk):
    treq = get_object_or_404(TreasuryOperationRequest, pk=pk)

    if not _treasury_staff_authorized(request.user):
        raise Http404("File not found.")

    # O.4a parity — MoneyBrokerAdminSite.admin_view() (admin.py) is the
    # only place TOTP_ADMIN_TREASURY_REQUIRED is currently enforced, and
    # it only wraps /admin/ routes. This view sits outside /admin/, so
    # without this block a Treasury operator who hasn't completed TOTP
    # could reach evidence bytes through here even while still blocked
    # from the admin change-list itself — replicate the same gate rather
    # than silently weakening it for this one access path.
    from .two_factor import totp_session_verified, treasury_2fa_required

    if getattr(settings, "TOTP_ADMIN_TREASURY_REQUIRED", False):
        if treasury_2fa_required(request.user) and not totp_session_verified(request):
            from .models import TOTPDevice

            request.session["2fa_next"] = request.get_full_path()
            has_confirmed_device = TOTPDevice.objects.filter(
                user=request.user, confirmed=True,
            ).exists()
            if has_confirmed_device:
                return redirect("simulator:totp_verify")
            return redirect("simulator:totp_setup")

    return _stream_field_file(treq.evidence)


def _broker_document_authorized(user, doc):
    if doc.public:
        return True
    return user.is_staff and (
        user.has_perm("simulator.view_brokerdocument")
        or user.has_perm("simulator.change_brokerdocument")
    )


@login_required
def secure_broker_document_view(request, doc_id):
    doc = get_object_or_404(BrokerDocument, pk=doc_id)

    if not _broker_document_authorized(request.user, doc):
        raise Http404("File not found.")

    return _stream_field_file(doc.file)


# ── Admin widget replacement ─────────────────────────────────────────────
#
# Django's default ClearableFileInput template renders
# "Currently: <a href="{{ widget.value.url }}">" for any editable FileField
# with an existing value — that .url call goes straight to
# FileSystemStorage.url() -> MEDIA_URL + name, bypassing every check above
# entirely. KYCProfileAdmin (document_front/document_back/selfie) and
# BrokerDocumentAdmin (file) both use the stock widget today. Rather than
# overriding Storage.url() globally (which would have to reverse-lookup an
# owning model instance from a bare storage name for every FileField in
# the project, including ones with no authorization story at all — a much
# larger and harder-to-audit surface), each of the two admin classes is
# given a widget bound to exactly which model/field it renders, so only
# these three fields' displayed links change.
#
# This only changes what URL is *displayed* in the admin form — the bytes
# behind that URL are still gated by the view functions above, which
# re-check authorization on every request regardless of how the link was
# produced. A storage-generated URL is never, by itself, sufficient to
# obtain the file.


class _SecureFieldFileProxy:
    """
    Thin wrapper around a FieldFile that overrides only `.url`, so the
    stock ClearableFileInput template renders our secure-view URL instead
    of the real storage URL. Every other attribute (name, size, __bool__,
    __str__) delegates to the wrapped FieldFile so the rest of the widget
    behaves exactly like it does for any other FileField.
    """

    def __init__(self, field_file, secure_url):
        self._field_file = field_file
        self._secure_url = secure_url

    def __getattr__(self, name):
        return getattr(self._field_file, name)

    def __bool__(self):
        return bool(self._field_file)

    def __str__(self):
        return str(self._field_file)

    @property
    def url(self):
        return self._secure_url


class SecureFileWidget(forms.ClearableFileInput):
    """
    ClearableFileInput that swaps the rendered "Currently: <a href>" link
    for a URL pointing at one of the secure views above, resolved from the
    FieldFile's own `.instance` (the model row Django attaches when the
    field is accessed off a real instance) — never from the raw storage
    name, so there is nothing path-like for the widget itself to leak.
    """

    def __init__(self, url_name, url_args_fn, **kwargs):
        self._url_name = url_name
        self._url_args_fn = url_args_fn
        super().__init__(**kwargs)

    def get_context(self, name, value, attrs):
        if value:
            args = self._url_args_fn(value)
            if args is not None:
                secure_url = reverse(self._url_name, args=args)
                value = _SecureFieldFileProxy(value, secure_url)
        return super().get_context(name, value, attrs)


def _kyc_widget_args(field_name):
    def _fn(field_file):
        instance = getattr(field_file, "instance", None)
        if instance is None or instance.pk is None:
            return None
        return [instance.pk, field_name]
    return _fn


def broker_document_widget_args(field_file):
    instance = getattr(field_file, "instance", None)
    if instance is None or instance.pk is None:
        return None
    return [instance.pk]


def kyc_secure_widget(field_name):
    return SecureFileWidget(
        url_name="simulator:secure_kyc_media",
        url_args_fn=_kyc_widget_args(field_name),
    )


def broker_document_secure_widget():
    return SecureFileWidget(
        url_name="simulator:secure_broker_document",
        url_args_fn=broker_document_widget_args,
    )
