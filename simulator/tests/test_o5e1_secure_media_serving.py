# simulator/tests/test_o5e1_secure_media_serving.py
"""
O.5e-1 — Secure Media Serving Foundation.

Closes RC-02 (Final Production Readiness Review): KYC documents, Treasury
evidence, and Broker documents had no safe way to be served in production
(Django's own static() helper only serves MEDIA_URL when DEBUG=True; no
Nginx `location /media/` exists either). This block adds three
authenticated/authorized Django views (simulator/secure_media.py) that
stream files from an authorized model instance, and rewires every known
`.url` consumer (documents.html template, KYCProfileAdmin's and
BrokerDocumentAdmin's file widgets, TreasuryOperationRequestAdmin's
readonly evidence display) to link to them instead.

Covers: KYC access matrix (owner/staff/anonymous/wrong field/missing
file), Treasury evidence access matrix (three Treasury permissions,
non-staff, O.4a TOTP-gate parity), BrokerDocument access matrix
(public=True/False x staff/non-staff), IDOR, malformed-field-value
handling (no path traversal surface exists to exploit, but the view must
still degrade to 404, not 500, for any unexpected value), symlink escape
containment, response-header safety for unicode/space filenames, and
regression checks that no admin/template surface still links to a raw
`.url`.
"""
import os
import shutil
import tempfile

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from simulator.models import BrokerDocument, KYCProfile, TreasuryOperationRequest

from .factories import make_user, make_wallet


def _grant(user, codename):
    user.user_permissions.add(Permission.objects.get(codename=codename))
    user.refresh_from_db()
    return user


def _fake_image(name="front.jpg", content=b"\xff\xd8\xff\xe0fake-jpeg-bytes"):
    return SimpleUploadedFile(name, content, content_type="image/jpeg")


def _make_kyc(user, **files):
    kyc, _ = KYCProfile.objects.get_or_create(user=user)
    for field, upload in files.items():
        setattr(kyc, field, upload)
    kyc.status = KYCProfile.STATUS_PENDING
    kyc.save()
    return kyc


def _make_treasury_request(evidence=None, **overrides):
    wallet = overrides.pop("wallet", None) or make_wallet()
    data = dict(
        operation_type=TreasuryOperationRequest.OP_BONUS_CREDIT,
        wallet=wallet,
        amount=25,
        reason="O.5e-1 secure media test",
    )
    data.update(overrides)
    req = TreasuryOperationRequest(**data)
    if evidence is not None:
        req.evidence = evidence
    req.save()
    return req


def _make_broker_document(public=True, upload=None):
    return BrokerDocument.objects.create(
        title="O.5e-1 test doc",
        file=upload or _fake_image("doc.pdf", b"%PDF-1.4 fake"),
        public=public,
    )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="trx_sim_o5e1_media_"))
class _MediaIsolatedTestCase(TestCase):
    """
    Base class that redirects MEDIA_ROOT to a throwaway temp directory for
    the duration of this file's tests, so uploads made here never touch
    the real project media/ folder and are guaranteed removed afterwards
    — needed for the symlink-escape test below, which must control the
    filesystem precisely, and kept for every test in this file for
    consistency/isolation.
    """

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(settings_media_root(), ignore_errors=True)
        super().tearDownClass()


def settings_media_root():
    from django.conf import settings
    return settings.MEDIA_ROOT


# ─────────────────────────────────────────────────────────────────────────
# KYC
# ─────────────────────────────────────────────────────────────────────────

class SecureKYCMediaViewTests(_MediaIsolatedTestCase):

    def setUp(self):
        self.owner = make_user(username="kyc_owner")
        self.other_user = make_user(username="kyc_other")
        self.staff_viewer = make_user(username="kyc_staff", is_staff=True)
        _grant(self.staff_viewer, "view_kycprofile")
        self.kyc = _make_kyc(
            self.owner,
            document_front=_fake_image("front.jpg"),
            selfie=_fake_image("selfie.jpg"),
        )
        self.client = Client()

    def _url(self, field, kyc_id=None):
        return reverse("simulator:secure_kyc_media", args=[kyc_id or self.kyc.pk, field])

    def test_owner_can_access_own_document_front(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), self.kyc.document_front.read())

    def test_owner_can_access_own_selfie(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._url("selfie"))
        self.assertEqual(resp.status_code, 200)

    def test_non_owner_non_staff_gets_404_not_403(self):
        self.client.force_login(self.other_user)
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 404)

    def test_staff_with_view_kycprofile_can_access_other_users_kyc(self):
        self.client.force_login(self.staff_viewer)
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 200)

    def test_staff_with_change_kycprofile_can_also_access(self):
        staff = make_user(username="kyc_staff_change", is_staff=True)
        _grant(staff, "change_kycprofile")
        self.client.force_login(staff)
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 200)

    def test_non_staff_user_with_perm_but_not_is_staff_still_blocked(self):
        # Defense in depth: is_staff is required in addition to the perm,
        # mirroring the admin's own effective requirement (AdminSite.
        # has_permission requires is_staff independently of has_perm()).
        non_staff = make_user(username="kyc_perm_no_staff", is_staff=False)
        _grant(non_staff, "view_kycprofile")
        self.client.force_login(non_staff)
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_user_redirected_to_login_not_404(self):
        resp = self.client.get(self._url("document_front"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("simulator:login"), resp.url)

    def test_invalid_field_name_returns_404(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._url("rejection_reason"))
        self.assertEqual(resp.status_code, 404)

    def test_field_name_with_traversal_like_value_returns_404(self):
        self.client.force_login(self.owner)
        resp = self.client.get(f"/secure-media/kyc/{self.kyc.pk}/../settings/")
        self.assertIn(resp.status_code, (301, 302, 404))

    def test_missing_file_field_returns_404_even_for_owner(self):
        self.client.force_login(self.owner)
        # document_back was never uploaded for self.kyc.
        resp = self.client.get(self._url("document_back"))
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_kyc_id_returns_404(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._url("document_front", kyc_id=999999))
        self.assertEqual(resp.status_code, 404)

    def test_unauthorized_response_does_not_leak_media_root_path(self):
        self.client.force_login(self.other_user)
        resp = self.client.get(self._url("document_front"))
        body = resp.content.decode(errors="ignore")
        self.assertNotIn(str(settings_media_root()), body)

    def test_content_disposition_header_present_on_success(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self._url("document_front"))
        self.assertIn("Content-Disposition", resp)
        self.assertIn("inline", resp["Content-Disposition"])

    def test_unicode_and_space_filename_does_not_break_response(self):
        user = make_user(username="kyc_unicode")
        kyc = _make_kyc(user, document_front=_fake_image("frente ñ documento (1).jpg"))
        self.client.force_login(user)
        resp = self.client.get(reverse(
            "simulator:secure_kyc_media", args=[kyc.pk, "document_front"],
        ))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Content-Disposition", resp)


# ─────────────────────────────────────────────────────────────────────────
# Treasury evidence
# ─────────────────────────────────────────────────────────────────────────

class SecureTreasuryEvidenceViewTests(_MediaIsolatedTestCase):

    def setUp(self):
        self.req = _make_treasury_request(evidence=_fake_image("evidence.pdf", b"%PDF fake"))
        self.client = Client()

    def _url(self, pk=None):
        return reverse("simulator:secure_treasury_evidence", args=[pk or self.req.pk])

    def test_submit_permission_holder_can_access(self):
        user = make_user(username="treasury_submit", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_review_permission_holder_can_access(self):
        user = make_user(username="treasury_review", is_staff=True)
        _grant(user, "can_review_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_execute_permission_holder_can_access(self):
        user = make_user(username="treasury_execute", is_staff=True)
        _grant(user, "can_execute_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_staff_without_any_treasury_permission_gets_404(self):
        user = make_user(username="treasury_no_perm", is_staff=True)
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 404)

    def test_non_staff_authenticated_user_gets_404(self):
        user = make_user(username="treasury_regular")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_user_redirected_to_login(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("simulator:login"), resp.url)

    def test_missing_evidence_returns_404(self):
        req = _make_treasury_request()  # no evidence attached
        user = make_user(username="treasury_submit2", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url(pk=req.pk))
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_request_id_returns_404(self):
        user = make_user(username="treasury_submit3", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url(pk=999999))
        self.assertEqual(resp.status_code, 404)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_totp_required_redirects_to_setup_without_confirmed_device(self):
        user = make_user(username="treasury_totp_setup", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("simulator:totp_setup"), resp.url)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_totp_required_redirects_to_verify_with_unverified_confirmed_device(self):
        import base64
        import pyotp
        from simulator.models import TOTPDevice

        user = make_user(username="treasury_totp_verify", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        TOTPDevice.objects.create(
            user=user,
            secret=f"b64:{base64.b64encode(pyotp.random_base32().encode()).decode()}",
            confirmed=True,
        )
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("simulator:totp_verify"), resp.url)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=True)
    def test_totp_verified_session_allows_access(self):
        user = make_user(username="treasury_totp_ok", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        session = self.client.session
        session["2fa_verified"] = True
        session.save()
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    @override_settings(TOTP_ADMIN_TREASURY_REQUIRED=False)
    def test_totp_flag_off_does_not_block_access(self):
        user = make_user(username="treasury_totp_off", is_staff=True)
        _grant(user, "can_submit_treasury_request")
        self.client.force_login(user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)


# ─────────────────────────────────────────────────────────────────────────
# Broker documents
# ─────────────────────────────────────────────────────────────────────────

class SecureBrokerDocumentViewTests(_MediaIsolatedTestCase):

    def setUp(self):
        self.client = Client()

    def _url(self, doc_id):
        return reverse("simulator:secure_broker_document", args=[doc_id])

    def test_authenticated_user_can_access_public_document(self):
        doc = _make_broker_document(public=True)
        user = make_user(username="doc_reader")
        self.client.force_login(user)
        resp = self.client.get(self._url(doc.pk))
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_user_redirected_to_login_for_public_document(self):
        doc = _make_broker_document(public=True)
        resp = self.client.get(self._url(doc.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("simulator:login"), resp.url)

    def test_non_staff_user_blocked_from_non_public_document(self):
        doc = _make_broker_document(public=False)
        user = make_user(username="doc_reader_blocked")
        self.client.force_login(user)
        resp = self.client.get(self._url(doc.pk))
        self.assertEqual(resp.status_code, 404)

    def test_staff_with_view_permission_can_access_non_public_document(self):
        doc = _make_broker_document(public=False)
        staff = make_user(username="doc_staff", is_staff=True)
        _grant(staff, "view_brokerdocument")
        self.client.force_login(staff)
        resp = self.client.get(self._url(doc.pk))
        self.assertEqual(resp.status_code, 200)

    def test_nonexistent_document_returns_404(self):
        user = make_user(username="doc_reader_404")
        self.client.force_login(user)
        resp = self.client.get(self._url(999999))
        self.assertEqual(resp.status_code, 404)

    def test_secure_url_property_matches_reverse(self):
        doc = _make_broker_document(public=True)
        self.assertEqual(
            doc.secure_url,
            reverse("simulator:secure_broker_document", args=[doc.pk]),
        )


# ─────────────────────────────────────────────────────────────────────────
# Security — IDOR, symlink escape, streaming
# ─────────────────────────────────────────────────────────────────────────

class SecureMediaSecurityTests(_MediaIsolatedTestCase):

    def test_idor_user_a_cannot_read_user_b_kyc_via_id_guessing(self):
        user_a = make_user(username="idor_a")
        user_b = make_user(username="idor_b")
        kyc_b = _make_kyc(user_b, document_front=_fake_image("b_front.jpg", b"secret-b-bytes"))

        self.client.force_login(user_a)
        resp = self.client.get(reverse(
            "simulator:secure_kyc_media", args=[kyc_b.pk, "document_front"],
        ))
        self.assertEqual(resp.status_code, 404)
        # Prove it's a real block, not an accidental miss: the same URL
        # works for the actual owner.
        self.client.force_login(user_b)
        resp_owner = self.client.get(reverse(
            "simulator:secure_kyc_media", args=[kyc_b.pk, "document_front"],
        ))
        self.assertEqual(resp_owner.status_code, 200)

    def test_symlink_escape_outside_media_root_is_blocked(self):
        """
        Plants a symlink inside MEDIA_ROOT/kyc/documents/ that resolves to
        a file OUTSIDE MEDIA_ROOT, then points a KYCProfile.document_front
        at that symlink's relative name directly (bypassing the upload
        path entirely, as a stand-in for "the DB row is somehow pointing
        at a symlink"). The view's realpath-containment check must refuse
        to serve it even though FileSystemStorage.path()/safe_join() has
        no '..' segment to reject here.
        """
        from django.conf import settings

        media_root = str(settings.MEDIA_ROOT)
        kyc_dir = os.path.join(media_root, "kyc", "documents")
        os.makedirs(kyc_dir, exist_ok=True)

        outside_dir = tempfile.mkdtemp(prefix="trx_sim_o5e1_outside_")
        self.addCleanup(shutil.rmtree, outside_dir, True)
        secret_path = os.path.join(outside_dir, "secret.txt")
        with open(secret_path, "wb") as fh:
            fh.write(b"outside-media-root-secret")

        symlink_name = "escape_symlink.jpg"
        symlink_path = os.path.join(kyc_dir, symlink_name)
        os.symlink(secret_path, symlink_path)
        self.addCleanup(lambda: os.path.exists(symlink_path) and os.remove(symlink_path))

        owner = make_user(username="symlink_owner")
        kyc, _ = KYCProfile.objects.get_or_create(user=owner)
        kyc.document_front.name = f"kyc/documents/{symlink_name}"
        kyc.status = KYCProfile.STATUS_PENDING
        kyc.save()

        self.client.force_login(owner)
        resp = self.client.get(reverse(
            "simulator:secure_kyc_media", args=[kyc.pk, "document_front"],
        ))
        self.assertEqual(resp.status_code, 404)

    def test_response_is_streaming_not_a_bare_httpresponse(self):
        from django.http import StreamingHttpResponse

        owner = make_user(username="streaming_owner")
        kyc = _make_kyc(owner, document_front=_fake_image("front.jpg"))
        self.client.force_login(owner)
        resp = self.client.get(reverse(
            "simulator:secure_kyc_media", args=[kyc.pk, "document_front"],
        ))
        self.assertIsInstance(resp, StreamingHttpResponse)

    def test_no_media_url_route_exists_when_debug_false(self):
        # trx_simulator/urls.py's `+ static(MEDIA_URL, ...)` only adds a
        # route when DEBUG=True — this is the RC-02 gap being closed, so
        # prove it stays true regardless of secure_media.py's addition.
        from django.conf import settings
        from django.urls import resolve
        from django.urls.exceptions import Resolver404

        self.assertFalse(settings.DEBUG)
        with self.assertRaises(Resolver404):
            resolve("/media/kyc/documents/anything.jpg")


# ─────────────────────────────────────────────────────────────────────────
# Regression — admin/template surfaces no longer expose raw `.url`
# ─────────────────────────────────────────────────────────────────────────

class SecureMediaAdminRegressionTests(_MediaIsolatedTestCase):

    def setUp(self):
        self.superuser = make_user(username="o5e1_admin", is_staff=True, is_superuser=True)
        self.client = Client()
        self.client.force_login(self.superuser)

    def test_kyc_admin_change_page_links_to_secure_view_not_media_url(self):
        owner = make_user(username="admin_regress_kyc")
        kyc = _make_kyc(owner, document_front=_fake_image("front.jpg"))
        url = reverse("admin:simulator_kycprofile_change", args=[kyc.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(
            reverse("simulator:secure_kyc_media", args=[kyc.pk, "document_front"]),
            body,
        )
        self.assertNotIn("/media/kyc/documents/", body)

    def test_broker_document_admin_change_page_links_to_secure_view(self):
        doc = _make_broker_document(public=True)
        url = reverse("admin:simulator_brokerdocument_change", args=[doc.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(
            reverse("simulator:secure_broker_document", args=[doc.pk]),
            body,
        )
        self.assertNotIn("/media/broker_documents/", body)

    def test_treasury_admin_change_page_links_to_secure_view(self):
        req = _make_treasury_request(evidence=_fake_image("evidence.pdf", b"%PDF fake"))
        url = reverse("admin:simulator_treasuryoperationrequest_change", args=[req.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn(
            reverse("simulator:secure_treasury_evidence", args=[req.pk]),
            body,
        )
        self.assertNotIn("/media/treasury/evidence/", body)

    def test_documents_view_template_uses_secure_url_not_file_url(self):
        _make_broker_document(public=True)
        user = make_user(username="doc_template_regress")
        self.client.logout()
        self.client.force_login(user)
        resp = self.client.get(reverse("simulator:documents"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("/secure-media/broker-document/", body)
        self.assertNotIn("/media/broker_documents/", body)

    def test_kyc_submission_page_has_no_media_url_link(self):
        # kyc.html only renders the upload form (raw <input type=file>),
        # never a link to an already-uploaded file — confirmed directly
        # against the template during O.5e-1 Fase 1 revalidation.
        user = make_user(username="kyc_form_regress")
        self.client.logout()
        self.client.force_login(user)
        resp = self.client.get(reverse("simulator:kyc"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("/media/", resp.content.decode())
