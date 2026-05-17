"""
Management command interface for the population simulation engine.

Usage examples:
    python manage.py populate_broker --preset mini --speed 5
    python manage.py populate_broker --normal 5 --gambler 3 --scalper 4 --duration 300
    python manage.py populate_broker --status
    python manage.py populate_broker --reset
    python manage.py populate_broker --clean
"""

import signal
import sys
import time

from django.core.management.base import BaseCommand, CommandError

from simulator.population_engine import PRESETS, PROFILES, PopulationRunner


class Command(BaseCommand):
    help = "Run the broker population simulation engine."

    def add_arguments(self, parser):
        # ── mutually exclusive operating modes ──────────────────────────────
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--status", action="store_true",
            help="Print current simulation state and exit.",
        )
        mode.add_argument(
            "--reset", action="store_true",
            help="Stop simulation, close all open positions, restore balances.",
        )
        mode.add_argument(
            "--clean", action="store_true",
            help="Stop simulation and delete ALL simulated accounts.",
        )

        # ── simulation parameters ────────────────────────────────────────────
        parser.add_argument(
            "--preset", choices=list(PRESETS.keys()),
            help="Load a predefined profile mix (mini/standard/stress).",
        )
        for p in PROFILES:
            parser.add_argument(
                f"--{p.lower()}", type=int, default=0,
                metavar="N",
                help=f"Number of {p} traders to spawn.",
            )
        parser.add_argument(
            "--speed", type=float, default=1.0,
            help="Time acceleration factor (0.5–50). Default: 1.0.",
        )
        parser.add_argument(
            "--tier", choices=["10K", "50K", "100K"], default="10K",
            help="Starting balance tier for all sim accounts. Default: 10K.",
        )
        parser.add_argument(
            "--duration", type=int, default=0,
            help="Auto-stop after N seconds (0 = run until Ctrl+C).",
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _print_status(self):
        s = PopulationRunner.status()
        self.stdout.write(
            self.style.HTTP_INFO(
                f"Running: {s['running']}  "
                f"Threads: {s['thread_count']}  "
                f"Accounts: {s['total_accounts']}"
            )
        )
        if not s["profiles"]:
            self.stdout.write("  (no simulated accounts found)")
            return
        for name, d in s["profiles"].items():
            self.stdout.write(
                f"  {name:<12} accounts={d['count']}  "
                f"active={d['active']}  suspended={d['suspended']}  "
                f"open_pos={d['open_positions']}  "
                f"balance=${d['balance_total']:,.2f}"
            )

    def _status_loop(self, threads, duration: int):
        """Print a status line every 10 s until duration or Ctrl+C."""
        start = time.monotonic()
        stopped = [False]

        def _sig_handler(sig, frame):
            stopped[0] = True

        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSimulation started — {len(threads)} thread(s). "
                f"Press Ctrl+C to stop.\n"
            )
        )

        while not stopped[0]:
            elapsed = int(time.monotonic() - start)
            s = PopulationRunner.status()
            total_pos = sum(d["open_positions"] for d in s["profiles"].values())
            total_bal = sum(d["balance_total"] for d in s["profiles"].values())
            total_acc = s["total_accounts"]
            suspended = sum(d["suspended"] for d in s["profiles"].values())

            self.stdout.write(
                f"[{elapsed:>5}s] "
                f"accounts={total_acc - suspended}/{total_acc}  "
                f"open_pos={total_pos}  "
                f"total_bal=${total_bal:,.0f}"
            )

            if duration and elapsed >= duration:
                self.stdout.write(self.style.WARNING(f"\nDuration {duration}s reached."))
                break

            # Sleep in 1-second steps so Ctrl+C and --duration are responsive
            for _ in range(10):
                if stopped[0]:
                    break
                time.sleep(1)
                if duration and int(time.monotonic() - start) >= duration:
                    break

        # Graceful shutdown
        self.stdout.write(self.style.WARNING("\nStopping simulation…"))
        PopulationRunner.stop()
        self.stdout.write(self.style.SUCCESS("Done."))

    # ── main ─────────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        # ── status ────────────────────────────────────────────────────────
        if options["status"]:
            self._print_status()
            return

        # ── reset ─────────────────────────────────────────────────────────
        if options["reset"]:
            n = PopulationRunner.reset()
            self.stdout.write(self.style.SUCCESS(f"Reset {n} account(s)."))
            return

        # ── clean ─────────────────────────────────────────────────────────
        if options["clean"]:
            n = PopulationRunner.delete_all()
            self.stdout.write(self.style.SUCCESS(f"Deleted {n} simulated account(s)."))
            return

        # ── build profile_counts ──────────────────────────────────────────
        if options.get("preset"):
            profile_counts = dict(PRESETS[options["preset"]])
        else:
            profile_counts = {
                p: options[p.lower()]
                for p in PROFILES
                if options[p.lower()] > 0
            }

        if not profile_counts:
            raise CommandError(
                "Specify a --preset or at least one profile count "
                "(e.g. --normal 3 --scalper 2)."
            )

        speed = max(0.5, min(options["speed"], 50.0))
        tier  = options["tier"]

        self.stdout.write(
            f"Starting simulation: {profile_counts}  "
            f"speed={speed}x  tier={tier}"
        )

        threads = PopulationRunner.start(
            profile_counts=profile_counts,
            speed=speed,
            tier=tier,
        )

        if not threads:
            raise CommandError("No threads started — check profile counts.")

        self._status_loop(threads, options["duration"])
