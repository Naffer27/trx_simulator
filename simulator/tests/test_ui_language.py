"""UI-only language preference: no database and no provider requests."""
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.template import Context, Template
from django.template.loader import get_template
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import include, path, reverse
from django.utils.translation import get_language, override


urlpatterns = [path("i18n/", include("django.conf.urls.i18n")), path("", lambda request: HttpResponse("ok"))]


class UILanguageTests(SimpleTestCase):
    def render(self, source, language=None, **values):
        request = RequestFactory().get("/")
        if language is not None:
            request.COOKIES[settings.LANGUAGE_COOKIE_NAME] = language
        return Template("{% load ui_language %}" + source).render(Context({"request": request, **values}))

    def test_default_and_cookie_and_invalid_cookie(self):
        for code, expected in ((None, "Iniciar sesión"), ("es", "Iniciar sesión"), ("en", "Login"), ("<script>", "Iniciar sesión")):
            with self.subTest(code=code):
                self.assertEqual(self.render('{% ui "Login" %}', code), expected)

    def test_numbers_and_active_locale_are_unchanged(self):
        source = '{% ui "Monto" %}:{{ amount }}|{{ amount|floatformat:2 }}'
        with override("en-us"):
            es = self.render(source, "es", amount=Decimal("1234.56"))
            en = self.render(source, "en", amount=Decimal("1234.56"))
            self.assertEqual(es.split(":")[1], en.split(":")[1])
            self.assertIn("1234.56", es)
            self.assertEqual(get_language(), "en-us")

    def test_unknown_content_is_preserved_and_html_escaped(self):
        self.assertEqual(self.render('{% ui value %}', "en", value='<script>alert("x")</script>'), '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;')

    def test_javascript_strings_cannot_close_script(self):
        text = self.render('{% ui_js value %}', "en", value='</script>"\n')
        self.assertNotIn("</script>", text)
        self.assertIn("\\u003C", text)
        self.assertIn("\\u0022", text)

    def test_error_display_preserves_unknown_errors_and_escapes_html(self):
        result = self.render('{% ui_errors errors %}', "en", errors=['<img src=x>', 'Unmapped provider message'])
        self.assertEqual(result, '&lt;img src=x&gt;, Unmapped provider message')

    def test_plural_phrases(self):
        source = '{% ui_plural count "posición abierta" "posiciones abiertas" %}'
        self.assertEqual(self.render(source, "en", count=2), "open positions")
        self.assertEqual(self.render(source, "es", count=1), "posición abierta")

    def test_all_modified_templates_compile(self):
        folder = Path(settings.BASE_DIR) / "simulator/templates/simulator"
        for filename in folder.rglob("*.html"):
            if "{% load ui_language %}" in filename.read_text():
                with self.subTest(template=filename.name):
                    get_template(str(filename.relative_to(folder.parent)))

    @override_settings(ROOT_URLCONF=__name__, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
    def test_selector_sets_cookie_and_blocks_external_redirect(self):
        client = Client()
        response = client.post(reverse("set_language"), {"language": "en", "next": "/?period=7d"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/?period=7d")
        self.assertEqual(response.cookies[settings.LANGUAGE_COOKIE_NAME].value, "en")
        response = client.post(reverse("set_language"), {"language": "es", "next": "https://evil.example/"})
        self.assertEqual(response["Location"], "/")

    @override_settings(ROOT_URLCONF=__name__, SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
    def test_language_switch_requires_csrf_and_valid_language(self):
        self.assertEqual(Client(enforce_csrf_checks=True).post(reverse("set_language"), {"language": "en"}).status_code, 403)
        response = Client().post(reverse("set_language"), {"language": "zz", "next": "/"})
        self.assertNotIn(settings.LANGUAGE_COOKIE_NAME, response.cookies)
