"""
Read-only audit: for every symbol in market_data/symbol_specs.py, evaluate
its market session (via market_data.sessions.evaluate_market_session) and
report calendar_id/state/order_policy.

Does NOT touch the DB. Does NOT make any network call — every calendar
here is a pure, declarative, in-memory rule (see market_data/sessions/
calendars.py); there is no external holiday vendor wired in.

Usage:
    python manage.py audit_market_sessions
    python manage.py audit_market_sessions --at "2026-07-12T15:00:00+00:00"
"""

from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.sessions.service import evaluate_market_session
from market_data.symbol_specs import get_all_specs


class Command(BaseCommand):
    help = "Read-only audit: what market session state does every SymbolSpec resolve to right now (or at --at)?"

    def add_arguments(self, parser):
        parser.add_argument(
            "--at", type=str, default=None,
            help="ISO 8601 timestamp to evaluate against, e.g. 2026-07-12T15:00:00+00:00 "
                 "(must include a UTC offset). Defaults to the real current time.",
        )

    def handle(self, *args, **options):
        at_raw = options["at"]
        if at_raw:
            try:
                now = datetime.fromisoformat(at_raw)
            except ValueError as exc:
                raise CommandError(f"--at is not a valid ISO 8601 timestamp: {at_raw!r} ({exc})")
            if now.tzinfo is None:
                raise CommandError(f"--at must include a UTC offset (got {at_raw!r})")
        else:
            now = datetime.now(timezone.utc)

        rows = []
        for spec in get_all_specs():
            profile = profile_from_symbol_spec(spec)
            result = evaluate_market_session(profile, now=now)
            rows.append(result)

        self.stdout.write(f"Evaluated at: {now.isoformat()}\n")
        self.stdout.write(f"  {'SYMBOL':<10} {'CALENDAR':<16} {'STATE':<12} {'ORDER_POLICY':<16} REASON")
        self.stdout.write("  " + "─" * 80)
        for r in rows:
            style = self.style.SUCCESS if r.state.value == "OPEN" else self.style.WARNING
            self.stdout.write(style(
                f"  {r.canonical_symbol:<10} {r.calendar_id.value:<16} {r.state.value:<12} "
                f"{r.order_policy.value:<16} {r.reason_code.value}"
            ))

        n_open = sum(1 for r in rows if r.state.value == "OPEN")
        self.stdout.write(f"\nTotal: {len(rows)}  OPEN={n_open}  OTHER={len(rows) - n_open}")
