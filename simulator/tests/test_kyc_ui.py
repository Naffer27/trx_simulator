# simulator/tests/test_kyc_ui.py
"""
KYC UI — regression tests (Part 2: form + view + template + sidebar).

Covers:
  1.  Anonymous user is redirected to login.
  2.  Authenticated user can load /kyc/ (200).
  3.  not_started status shows the editable form.
  4.  POST with valid data creates KYCProfile with status=pending.
  5.  POST sets submitted_at.
  6.  POST redirects back to /kyc/.
  7.  pending status does NOT show the editable form.
  8.  approved status does NOT show the editable form.
  9.  rejected status shows the rejection_reason.
  10. rejected status shows the editable form again.
  11. Resubmitting (rejected → POST) clears review fields and sets pending.
  12. Template contains {% csrf_token %} (csrfmiddlewaretoken).
  13. Template form has enctype="multipart/form-data".
  14. Sidebar contains the /kyc/ link and "Verificación KYC" text.
"""
import io
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone

from simulator.models import KYCProfile
from simulator.tests.factories import make_user

User = get_user_model()

KYC_URL = "/kyc/"


def _fake_image(name="front.jpg") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0" + b"\x00" * 20, content_type="image/jpeg")


def _valid_post(front_file=None):
    return {
        "legal_name":    "Juan Pérez",
        "country":       "Venezuela",
        "document_type": "national_id",
        "document_number": "",
        "document_front": front_file or _fake_image(),
    }


class KYCAnonTests(TestCase):
    def test_anon_redirects_to_login(self):
        resp = self.client.get(KYC_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_anon_post_redirects_to_login(self):
        resp = self.client.post(KYC_URL, _valid_post())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])


class KYCGetTests(TestCase):
    def setUp(self):
        self.user = make_user(username="kyc_get_user")
        self.client.force_login(self.user)

    def test_authenticated_user_gets_200(self):
        resp = self.client.get(KYC_URL)
        self.assertEqual(resp.status_code, 200)

    def test_not_started_shows_form(self):
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertIn("document_front", content)
        self.assertIn("legal_name", content)

    def test_template_has_csrf_token(self):
        resp = self.client.get(KYC_URL)
        self.assertIn("csrfmiddlewaretoken", resp.content.decode())

    def test_template_form_has_multipart(self):
        resp = self.client.get(KYC_URL)
        self.assertIn("multipart/form-data", resp.content.decode())


class KYCPostValidTests(TestCase):
    def setUp(self):
        self.user = make_user(username="kyc_post_user")
        self.client.force_login(self.user)

    def test_valid_post_creates_kyc_pending(self):
        self.client.post(KYC_URL, _valid_post())
        kyc = KYCProfile.objects.get(user=self.user)
        self.assertEqual(kyc.status, KYCProfile.STATUS_PENDING)

    def test_valid_post_sets_submitted_at(self):
        self.client.post(KYC_URL, _valid_post())
        kyc = KYCProfile.objects.get(user=self.user)
        self.assertIsNotNone(kyc.submitted_at)

    def test_valid_post_redirects_to_kyc(self):
        resp = self.client.post(KYC_URL, _valid_post())
        self.assertRedirects(resp, KYC_URL, fetch_redirect_response=False)

    def test_valid_post_saves_legal_name(self):
        self.client.post(KYC_URL, _valid_post())
        kyc = KYCProfile.objects.get(user=self.user)
        self.assertEqual(kyc.legal_name, "Juan Pérez")

    def test_invalid_post_missing_required_field_stays_on_form(self):
        resp = self.client.post(KYC_URL, {
            "legal_name":    "",
            "country":       "Venezuela",
            "document_type": "passport",
            "document_front": _fake_image(),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(KYCProfile.objects.filter(
            user=self.user, status=KYCProfile.STATUS_PENDING
        ).exists())


class KYCStatusDisplayTests(TestCase):
    def setUp(self):
        self.user = make_user(username="kyc_status_display_user")
        self.client.force_login(self.user)

    def _set_status(self, status, **kwargs):
        kyc, _ = KYCProfile.objects.get_or_create(user=self.user)
        kyc.status = status
        for k, v in kwargs.items():
            setattr(kyc, k, v)
        kyc.save()
        return kyc

    def test_pending_does_not_show_editable_form(self):
        self._set_status(KYCProfile.STATUS_PENDING)
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertNotIn('name="document_front"', content)
        self.assertNotIn('name="legal_name"', content)

    def test_pending_shows_in_review_message(self):
        self._set_status(KYCProfile.STATUS_PENDING)
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertIn("revisión", content.lower())

    def test_approved_does_not_show_editable_form(self):
        self._set_status(KYCProfile.STATUS_APPROVED)
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertNotIn('name="document_front"', content)

    def test_approved_shows_verified_message(self):
        self._set_status(KYCProfile.STATUS_APPROVED)
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertIn("Verificad", content)

    def test_rejected_shows_rejection_reason(self):
        self._set_status(KYCProfile.STATUS_REJECTED, rejection_reason="Documento ilegible")
        resp = self.client.get(KYC_URL)
        self.assertContains(resp, "Documento ilegible")

    def test_rejected_shows_editable_form(self):
        self._set_status(KYCProfile.STATUS_REJECTED)
        resp = self.client.get(KYC_URL)
        content = resp.content.decode()
        self.assertIn('name="document_front"', content)

    def test_rejected_post_clears_review_fields(self):
        reviewer = make_user(username="kyc_reviewer_disp")
        reviewer.is_staff = True
        reviewer.save()
        self._set_status(
            KYCProfile.STATUS_REJECTED,
            reviewed_at=timezone.now(),
            reviewed_by=reviewer,
            rejection_reason="Foto borrosa",
        )
        self.client.post(KYC_URL, _valid_post())
        kyc = KYCProfile.objects.get(user=self.user)
        self.assertEqual(kyc.status, KYCProfile.STATUS_PENDING)
        self.assertIsNone(kyc.reviewed_at)
        self.assertIsNone(kyc.reviewed_by)
        self.assertEqual(kyc.rejection_reason, "")


class KYCSidebarTests(TestCase):
    def setUp(self):
        self.user = make_user(username="kyc_sidebar_user")
        self.client.force_login(self.user)

    def test_sidebar_contains_kyc_link(self):
        resp = self.client.get("/accounts/")
        content = resp.content.decode()
        self.assertIn("/kyc/", content)

    def test_sidebar_contains_kyc_label(self):
        resp = self.client.get("/accounts/")
        content = resp.content.decode()
        self.assertIn("Verificación KYC", content)
