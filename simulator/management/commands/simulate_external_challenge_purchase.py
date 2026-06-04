"""
Simulate an external sales platform sending a signed challenge activation
webhook to Money Broker's internal endpoint.

Usage:
    python manage.py simulate_external_challenge_purchase \\
        --email testbuyer@example.com \\
        --name "Test Buyer" \\
        --code TEST_CHALLENGE_10K_2PHASE \\
        --amount 1.00

    # Against a remote server:
    python manage.py simulate_external_challenge_purchase \\
        --email testbuyer@example.com \\
        --name "Test Buyer" \\
        --code TEST_CHALLENGE_10K_2PHASE \\
        --amount 1.00 \\
        --url https://staging.moneybroker.com

Reads CHALLENGE_WEBHOOK_SECRET from Django settings.
Exits with code 1 if the secret is not configured or the request fails.
"""
import hashlib
import hmac
import json
import sys
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

ENDPOINT_PATH = "/api/internal/challenge/activate/"


def _sign(payload: dict, secret: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class Command(BaseCommand):
    help = "Simulate an external platform sending a signed challenge purchase webhook."

    def add_arguments(self, parser):
        parser.add_argument("--email",  required=True, help="Buyer email address.")
        parser.add_argument("--name",   default="Test Buyer", help="Buyer full name.")
        parser.add_argument("--code",   default="TEST_CHALLENGE_10K_2PHASE",
                            help="challenge_product_code (external_code on ChallengeProduct).")
        parser.add_argument("--amount", default="1.00", type=str,
                            help="amount_paid in USD.")
        parser.add_argument("--url",    default="http://127.0.0.1:8000",
                            help="Base URL of the Money Broker server.")
        parser.add_argument("--event-id", default=None,
                            help="Override event_id (default: random UUID4).")
        parser.add_argument("--payment-id", default=None,
                            help="Override payment_id (default: random UUID4).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the signed payload without sending the request.")

    def handle(self, *args, **options):
        secret = getattr(settings, "CHALLENGE_WEBHOOK_SECRET", "").strip()
        if not secret:
            raise CommandError(
                "CHALLENGE_WEBHOOK_SECRET is not set in settings / .env.\n"
                "Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
                "Then add it to your .env:\n"
                "  CHALLENGE_WEBHOOK_SECRET=<generated_value>"
            )

        event_id   = options["event_id"]   or str(uuid.uuid4())
        payment_id = options["payment_id"] or str(uuid.uuid4())
        paid_at    = datetime.now(timezone.utc).isoformat()

        payload = {
            "event_id":               event_id,
            "email":                  options["email"],
            "full_name":              options["name"],
            "challenge_product_code": options["code"],
            "payment_id":             payment_id,
            "amount_paid":            float(options["amount"]),
            "currency":               "USD",
            "paid_at":                paid_at,
        }

        signature = _sign(payload, secret)
        body      = json.dumps(payload)

        self.stdout.write("")
        self.stdout.write("── Payload ──────────────────────────────────────")
        self.stdout.write(json.dumps(payload, indent=2))
        self.stdout.write("")
        self.stdout.write(f"── Signature (X-MoneyBroker-Signature) ──────────")
        self.stdout.write(signature)
        self.stdout.write("")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — request NOT sent."))
            return

        url = options["url"].rstrip("/") + ENDPOINT_PATH
        self.stdout.write(f"── POST {url} ────────────────────────────────────")

        import urllib.request
        req = urllib.request.Request(
            url=url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type":             "application/json",
                "X-MoneyBroker-Signature":  signature,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status  = resp.status
                content = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status  = exc.code
            content = exc.read().decode("utf-8")
        except Exception as exc:
            raise CommandError(f"Request failed: {exc}")

        self.stdout.write(f"── Response HTTP {status} ────────────────────────────")
        try:
            parsed = json.loads(content)
            self.stdout.write(json.dumps(parsed, indent=2))
        except json.JSONDecodeError:
            self.stdout.write(content)
        self.stdout.write("")

        if status == 200:
            parsed = json.loads(content)
            if parsed.get("idempotent"):
                self.stdout.write(self.style.WARNING(
                    "IDEMPOTENT — enrollment already existed (duplicate event_id or payment_id)."
                ))
            else:
                self.stdout.write(self.style.SUCCESS("SUCCESS — enrollment created."))
                self.stdout.write(f"  enrollment_id : {parsed.get('enrollment_id')}")
                self.stdout.write(f"  account_id    : {parsed.get('account_id')}")
                self.stdout.write(f"  user_created  : {parsed.get('user_created')}")
                self.stdout.write(f"  login_url     : {parsed.get('login_url')}")
        else:
            self.stderr.write(self.style.ERROR(f"FAILED — HTTP {status}"))
            sys.exit(1)
