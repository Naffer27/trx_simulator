"""
Read-only audit: compare market_data/symbol_specs.py (the live runtime
source) against simulator.models.Instrument (the DB catalog) via the common
InstrumentProfile contract (market_data/instruments/), and report drift.

Does NOT write to the DB. Does NOT modify any Instrument row. Does NOT
change which source governs live trading — SymbolSpec remains the runtime
source of truth regardless of what this command reports (see
docs/MARKET_DATA_ARCHITECTURE.md MD-1 §2). There is no --fix option.

Usage:
    python manage.py audit_instrument_profiles
    python manage.py audit_instrument_profiles --strict
"""

from django.core.management.base import BaseCommand

from market_data.instruments import compare_profiles, profile_from_instrument, profile_from_symbol_spec
from market_data.instruments.bridges import provider_mappings_for_instrument
from market_data.symbol_specs import get_all_specs
from simulator.models import Instrument


class Command(BaseCommand):
    help = (
        "Read-only audit comparing SymbolSpec (runtime) against Instrument (DB) "
        "for drift. Never writes to the DB — reporting only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--strict", action="store_true",
            help="Exit with a non-zero status if any symbol has critical drift.",
        )

    def handle(self, *args, **options):
        strict = options["strict"]

        runtime_profiles = {
            spec.symbol: profile_from_symbol_spec(spec) for spec in get_all_specs()
        }

        db_profiles = {}
        for instrument in Instrument.objects.all():  # read-only — no .save()/.update() anywhere below
            mappings = provider_mappings_for_instrument(instrument)
            profile = profile_from_instrument(instrument, provider_mappings=mappings)
            db_profiles[profile.canonical_symbol] = profile

        all_symbols = sorted(set(runtime_profiles) | set(db_profiles))

        rows = []
        any_critical = False

        for symbol in all_symbols:
            runtime_profile = runtime_profiles.get(symbol)
            db_profile = db_profiles.get(symbol)

            if runtime_profile is not None and db_profile is not None:
                report = compare_profiles(runtime_profile, db_profile)
                if report.critical_differences:
                    status = "DRIFT"
                    any_critical = True
                elif report.warning_differences:
                    status = "DRIFT"
                else:
                    status = "MATCH"
                rows.append((symbol, status, report))
            elif runtime_profile is not None:
                rows.append((symbol, "ONLY_RUNTIME", None))
            else:
                rows.append((symbol, "ONLY_DB", None))

        self._print_table(rows)

        if strict and any_critical:
            self.stderr.write(self.style.ERROR(
                "\n--strict: exiting non-zero — one or more symbols have critical drift."
            ))
            raise SystemExit(1)

    def _print_table(self, rows):
        self.stdout.write(
            f"  {'SYMBOL':<10} {'STATUS':<14} {'CRITICAL':>8} {'WARNING':>8}"
        )
        self.stdout.write("  " + "─" * 46)

        for symbol, status, report in rows:
            n_critical = len(report.critical_differences) if report else 0
            n_warning = len(report.warning_differences) if report else 0
            style = self.style.SUCCESS if status == "MATCH" else (
                self.style.ERROR if status == "DRIFT" and n_critical else self.style.WARNING
            )
            self.stdout.write(style(
                f"  {symbol:<10} {status:<14} {n_critical:>8} {n_warning:>8}"
            ))

        self.stdout.write("")
        for symbol, status, report in rows:
            if report is None or not report.differences:
                continue
            self.stdout.write(f"  {symbol} — drift detail:")
            for diff in report.differences:
                self.stdout.write(
                    f"      [{diff.severity:<8}] {diff.field:<32} "
                    f"runtime={diff.runtime_value!r}  db={diff.db_value!r}"
                )

        n_match = sum(1 for _, s, _ in rows if s == "MATCH")
        n_drift = sum(1 for _, s, _ in rows if s == "DRIFT")
        n_only_runtime = sum(1 for _, s, _ in rows if s == "ONLY_RUNTIME")
        n_only_db = sum(1 for _, s, _ in rows if s == "ONLY_DB")
        self.stdout.write(
            f"\nTotal: {len(rows)}  MATCH={n_match}  DRIFT={n_drift}  "
            f"ONLY_RUNTIME={n_only_runtime}  ONLY_DB={n_only_db}"
        )
