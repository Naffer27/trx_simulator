"""Translate presentation text without changing Django's numeric locale.

The standard set_language view owns the cookie. Deliberately do not activate
LocaleMiddleware: financial form parsing, JSON and numeric template values
retain their existing locale. This module never writes account preferences.
"""
import json
from functools import lru_cache
from pathlib import Path

from django import template
from django.conf import settings
from django.utils.html import escapejs, format_html_join

register = template.Library()


@lru_cache(maxsize=1)
def _catalog():
    return json.loads((Path(__file__).resolve().parent.parent / "ui_translations.json").read_text(encoding="utf-8"))


def _language(context):
    request = context.get("request")
    value = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, "es") if request else "es"
    return value if value in ("es", "en") else "es"


@register.simple_tag(takes_context=True)
def ui_language(context):
    return _language(context)


@register.simple_tag(takes_context=True)
def ui(context, source):
    # simple_tag escapes the result; never mark translated strings safe.
    source = str(source)
    return _catalog().get(source, {}).get(_language(context), source)


@register.simple_tag(takes_context=True)
def ui_js(context, source):
    """For a quoted JavaScript string only; escape quotes and script delimiters."""
    return escapejs(ui(context, source))


@register.simple_tag(takes_context=True)
def ui_plural(context, count, singular, plural):
    return ui(context, singular if count == 1 else plural)


@register.simple_tag(takes_context=True)
def ui_errors(context, errors):
    """Display existing validation results; do not change or suppress errors."""
    return format_html_join(", ", "{}", ((ui(context, error),) for error in errors))
