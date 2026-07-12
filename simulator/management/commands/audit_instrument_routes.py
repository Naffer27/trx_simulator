"""
Read-only audit: for every symbol in market_data/symbol_specs.py, attempt to
build a ProviderRoutePlan via market_data.instruments.routing.build_route_plan()
and report OK/INVALID.

Does NOT touch the DB — scoped to the runtime (SymbolSpec-derived) side
only. Validating routes built from the DB catalog too is deferred to a
later block, per FOUNDATION-07's explicit "keep this simple or defer"
allowance — this command already answers the question that matters today:
"does every live-registered instrument produce a buildable route plan?"

Usage:
    python manage.py audit_instrument_routes
    python manage.py audit_instrument_routes --strict
"""

from django.core.management.base import BaseCommand

from market_data.instruments import profile_from_symbol_spec
from market_data.instruments.routing import build_route_plan
from market_data.symbol_specs import get_all_specs


class Command(BaseCommand):
    help = "Read-only audit: can every SymbolSpec build a valid ProviderRoutePlan?"

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict", action="store_true",
            help="Exit with a non-zero status if any symbol fails to build a route plan.",
        )

    def handle(self, *args, **options):
        strict = options["strict"]
        rows = []
        any_invalid = False

        for spec in get_all_specs():
            profile = profile_from_symbol_spec(spec)
            try:
                plan = build_route_plan(profile)
            except ValueError as exc:
                rows.append((profile.canonical_symbol, "INVALID", str(exc)))
                any_invalid = True
                continue

            if plan.entries:
                detail = ", ".join(f"{e.provider_id}(p{e.priority})" for e in plan.entries)
            else:
                detail = "simulation-only" if plan.simulation_allowed else "no providers"
            rows.append((profile.canonical_symbol, "OK", detail))

        self.stdout.write(f"  {'SYMBOL':<10} {'STATUS':<10} DETAIL")
        self.stdout.write("  " + "─" * 60)
        for symbol, status, detail in rows:
            style = self.style.SUCCESS if status == "OK" else self.style.ERROR
            self.stdout.write(style(f"  {symbol:<10} {status:<10} {detail}"))

        n_ok = sum(1 for _, status, _ in rows if status == "OK")
        n_invalid = len(rows) - n_ok
        self.stdout.write(f"\nTotal: {len(rows)}  OK={n_ok}  INVALID={n_invalid}")

        if strict and any_invalid:
            self.stderr.write(self.style.ERROR(
                "\n--strict: exiting non-zero — one or more symbols failed to build a route plan."
            ))
            raise SystemExit(1)
