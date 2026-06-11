# Pre-launch Security Checklist

Complete these steps in order before opening the application to real users.
Each step has a verification command or action you can run to confirm it.

---

## 1. Generate a strong `DJANGO_SECRET_KEY`

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Set the output in `.env` as `DJANGO_SECRET_KEY=...`.
Never reuse a key from another project or from `.env.example`.

---

## 2. Set `DOMAIN` and `SITE_URL`

In `.env`:

```
DOMAIN=yourdomain.com
SITE_URL=https://yourdomain.com
```

- `DOMAIN` is added to `ALLOWED_HOSTS`. Wrong value → Django rejects all requests.
- `SITE_URL` is embedded in every outgoing email link (password reset, withdrawal confirmations, etc.). Wrong value → broken links in emails.

---

## 3. Configure real email delivery

Set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
`DEFAULT_FROM_EMAIL`, `SERVER_EMAIL`, and `ADMIN_EMAIL` for a dedicated transactional provider
(SendGrid, Mailgun, Postmark, Amazon SES, etc.).

**Do not use Gmail SMTP for production.** It has low daily limits and is flagged as spam by most providers.

Smoke-test email delivery after deploying:

```bash
python manage.py shell -c "
from django.core.mail import send_mail
send_mail('smoke test', 'it works', None, ['you@example.com'])
print('sent')
"
```

---

## 4. Set `NOWPAYMENTS_IPN_SECRET` to a strong secret

Generate with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set the **same value** in both `.env` and your NowPayments dashboard → IPN settings.
A mismatched secret causes all payment callbacks to be rejected silently.

---

## 5. Confirm `DEBUG=False`

```bash
grep DEBUG .env
```

`DEBUG=True` leaks full stack traces, settings, and local variables to any visitor.
**Never deploy with `DEBUG=True`.**

---

## 6. Confirm HTTPS is working end-to-end

Visit `https://yourdomain.com` in a browser. Verify:

- The padlock icon is shown.
- The TLS certificate is valid and not expired.
- There are no mixed-content warnings.

Only proceed to step 7 after HTTPS is confirmed.

---

## 7. Enable HSTS after HTTPS is confirmed

In `.env`:

```
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True
```

**Warning:** Once HSTS is active, browsers will refuse HTTP connections for the duration
of `SECURE_HSTS_SECONDS`. Do not enable this until you are certain HTTPS will remain
available — it cannot be undone immediately.

---

## 8. Change `ADMIN_URL` to an unpredictable path

Pick a path that is not guessable by automated scanners:

```
ADMIN_URL=mb-ops-a1b2c3
```

This alone is not authentication, but it eliminates automated brute-force login attempts
that target `/admin/` on every Django site. Confirm the old `/admin/` path returns 404.

---

## 9. Confirm `media/` is not tracked by git

```bash
git status --short | grep media/
```

Should return nothing. Verify `.gitignore` contains `media/`. User-uploaded files (KYC
documents, profile photos) must never be committed.

---

## 10. Run Django's production check

```bash
DEBUG=False DJANGO_SECRET_KEY=<your-key> DOMAIN=yourdomain.com \
  EMAIL_HOST=smtp.example.com NOWPAYMENTS_IPN_SECRET=<your-secret> \
  SITE_URL=https://yourdomain.com \
  python manage.py check --deploy
```

Address any `CRITICAL` or `ERROR` level issues before launch. `WARNING` level items
related to HSTS/SSL are acceptable if HTTPS is not yet live (see steps 6–7).

---

## 11. Run the full test suite

```bash
python manage.py test simulator.tests -v 1
```

All tests must pass on the production branch before deploying. A failing test on
main means a regression has been introduced.

---

## 12. Smoke test the full user journey

After deploying to production, manually verify each step:

1. **Registration** — register a new account; confirm the verification email arrives and the link works.
2. **Login** — log in with the new account.
3. **Deposit** — initiate a test deposit; verify the NowPayments redirect works.
4. **Account / Dashboard** — confirm balance and transaction history render.
5. **KYC** — submit a KYC document; confirm it appears in the admin for review.
6. **Withdrawal** — submit a withdrawal request; confirm the confirmation email arrives and the request appears in the admin.

If any step fails, roll back and investigate before opening the application to users.

---

## Additional reminders

| Setting | Must be in production |
|---|---|
| `LOAD_TEST_MODE` | `False` or unset — `True` disables all rate limiting |
| `TOTP_ENCRYPTION_KEY` | A real Fernet key (see `.env.example`) |
| `CHALLENGE_WEBHOOK_SECRET` | A strong random secret shared with the webhook sender |
| `BROKER_ACCESS_CODE` | Changed from the default (if access-code mode is enabled) |
| `SENTRY_DSN` | Set for error monitoring in production |
