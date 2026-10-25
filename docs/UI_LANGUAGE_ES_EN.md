# Client UI language selector

Local implementation: Spanish by default, with ES/EN selection stored in the standard Django language cookie. The selector uses Django's CSRF-protected set_language endpoint and returns to the current page.

## Scope

Client navigation, sign-in/registration/recovery, account overview/opening, deposits, withdrawals and wallet registration, KYC, profile and 2FA, history, support pages, public landing, informational pages, challenges and associates pages use a presentation dictionary.

The trading terminal, operator/admin screens, email templates, embedded support-chat fragments and user-authored content are not covered. Unknown dynamic messages remain unchanged. This is not a claim of complete site localization.

## Boundary

No LocaleMiddleware or global locale activation was added. LANGUAGE_CODE remains en-us. Existing number formatting, form field names/values, routes, validation, amounts, payment provider calls and business logic remain unchanged. No database migration or preference field is needed.

The only existing Python changes register the language endpoint and cookie options. New template tags read simulator/ui_translations.json; include that file in any deployment. No production deployment was performed.

## Validation

Run: python manage.py test simulator.tests.test_ui_language

Tests cover default/selected/invalid languages, decimal preservation, HTML/JavaScript escaping, error presentation, plural phrases, template compilation, safe redirects and CSRF. Isolated previews of nine pages in both languages rendered with database access blocked. Form attributes match across languages; 64 rendered inline scripts parsed successfully. Mobile previews checked at 390px.
