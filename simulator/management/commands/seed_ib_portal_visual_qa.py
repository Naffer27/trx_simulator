# simulator/management/commands/seed_ib_portal_visual_qa.py
"""
IB-PORTAL-UX-10A — LOCAL VISUAL QA DATA ONLY.

Populates the /associates/ dashboard for a real, existing IB user with
enough real, correctly-attributed activity to inspect the dashboard
visually — every widget still reads the exact same certified SSOT/
queries as production; this command only creates the underlying rows,
via the same real services production code itself uses wherever one
exists (credit_wallet()/debit_wallet() for money movement,
change_commission_rate()/sweep_per_lot()/approve_obligation()/
hold_obligation() for the IB commission engine). No new economic
formula, no second calculation, anywhere in this file.

Usage:
    python manage.py seed_ib_portal_visual_qa --user Admin2
    python manage.py seed_ib_portal_visual_qa --user Admin2 --cleanup

Identification: every trader/reviewer User this command creates has a
username starting with QA_MARKER ("qa10a_"). --cleanup deletes ONLY
rows reachable from those QA-marked users, in FK-safe order — it never
touches the target IB's own pre-existing Referral, real users, or any
other data. Refuses to seed twice without a --cleanup in between
(idempotency guard), and refuses to run against a --user that doesn't
already exist (never creates the target IB).

PRODUCTION GUARD (fail-closed, positive allowlist): this command
refuses to run at all — seed OR cleanup — unless settings.APP_ENV is
in _ALLOWED_APP_ENVS. Mirrors the exact allowlist discipline this
codebase already uses elsewhere (e.g. TradingAccount.
WITHDRAWABLE_ACCOUNT_TYPES) rather than a denylist of "known-bad"
environments, which a future unlisted environment name could silently
slip past. Checked first, before any argument parsing that could touch
the database. Does not change what the command does once allowed to
run — only whether it runs at all.
"""
import random
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.test import RequestFactory
from django.utils import timezone

QA_MARKER = "qa10a_"
QA_REVIEWER_USERNAME = f"{QA_MARKER}reviewer"
CLICKS_STASH_PREFIX = "clicks_delta:"

# Fail-closed positive allowlist — see PRODUCTION GUARD above. Anything
# not explicitly listed here (including "staging", "production", an
# unset/blank value, or a future/unrecognized environment name) is
# refused, never allowed by default.
_ALLOWED_APP_ENVS = frozenset({"development", "dev", "local", "test"})

User = get_user_model()


class Command(BaseCommand):
    help = "IB-PORTAL-UX-10A — seed/cleanup local-only visual QA data for /associates/. Local/dev only."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Username of the existing IB to populate (e.g. Admin2).")
        parser.add_argument("--cleanup", action="store_true", help="Delete all QA data created by this command.")

    def handle(self, *args, **options):
        app_env = str(getattr(settings, "APP_ENV", "")).strip().lower()
        if app_env not in _ALLOWED_APP_ENVS:
            raise CommandError(
                f"Refusing to run: settings.APP_ENV={app_env!r} is not in the allowed "
                f"local/dev set {sorted(_ALLOWED_APP_ENVS)}. This command writes real "
                "Deposit/WithdrawalRequest/WalletTransaction/IBCommissionRule/"
                "IBCommissionObligation rows via real services and must never run "
                "against staging or production, by explicit Owner directive "
                "(IB-PORTAL-UX-10A pre-closure). This check applies to --cleanup too."
            )

        target_username = options["user"]
        target_user = User.objects.filter(username=target_username).first()
        if target_user is None:
            raise CommandError(
                f"User {target_username!r} does not exist — this command never creates "
                "the target IB, only QA trader/activity data attributed to them."
            )

        if options["cleanup"]:
            self._cleanup()
            return

        self._seed(target_user)

    # ─────────────────────────────────────────────────────────────────
    # CLEANUP — explicit FK-safe order (PROTECT relations first).
    # ─────────────────────────────────────────────────────────────────

    def _cleanup(self):
        from simulator.models import (
            Deposit, IBCommissionObligation, IBCommissionRule, IBRiskEvent, LotExecutionEvent,
            Position, Referral, ReferralAttribution, Trade, TradingAccount, Wallet,
            WalletTransaction, WithdrawalRequest,
        )

        with transaction.atomic():
            counts = {}

            # ── Reverse the ONE pre-existing real object this command
            # mutates (Referral.clicks — a single counter, no per-click
            # row exists to create/delete instead). The exact delta was
            # stashed on the QA reviewer's last_name at seed time; read
            # it back BEFORE that user is deleted below. Every other
            # attributed IB is left untouched — only the referral(s)
            # actually bumped by a QA reviewer this command created. ──
            reviewer = User.objects.filter(
                username=QA_REVIEWER_USERNAME, last_name__startswith=CLICKS_STASH_PREFIX,
            ).first()
            clicks_reverted = 0
            if reviewer is not None:
                try:
                    delta = int(reviewer.last_name[len(CLICKS_STASH_PREFIX):])
                except (ValueError, TypeError):
                    delta = 0
                # The stash doesn't record WHICH referral was bumped (only
                # one QA run/target IB is supported at a time — see the
                # idempotency guard in _seed()), so the single IB actually
                # touched is unambiguous: it's whichever real Referral
                # currently has one or more QA-marked ReferralAttribution
                # rows pointing at it.
                touched_ref = Referral.objects.filter(
                    attributions__referred_user__username__startswith=QA_MARKER,
                ).distinct().first()
                if touched_ref is not None and delta > 0:
                    Referral.objects.filter(pk=touched_ref.pk, clicks__gte=delta).update(
                        clicks=F("clicks") - delta,
                    )
                    clicks_reverted = delta
            counts["Referral.clicks reverted by"] = clicks_reverted

            # IBRiskEvent.obligation is PROTECT — must go before the
            # IBCommissionObligation rows it references (hold_obligation()
            # writes one of these for every QA-obligation hold below).
            # Scoped only to events tied to a QA-created obligation —
            # never a referral-scoped (FROZEN/UNFROZEN, obligation=None)
            # event, which would never belong to this QA run anyway.
            counts["IBRiskEvent"] = IBRiskEvent.objects.filter(
                obligation__attribution__referred_user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["IBCommissionObligation"] = IBCommissionObligation.objects.filter(
                attribution__referred_user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["IBCommissionRule (per-IB, QA reviewer-created)"] = IBCommissionRule.objects.filter(
                created_by__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["LotExecutionEvent"] = LotExecutionEvent.objects.filter(
                account__user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["Position"] = Position.objects.filter(
                account__user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["Trade"] = Trade.objects.filter(
                account__user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["WalletTransaction"] = WalletTransaction.objects.filter(
                wallet__user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["WithdrawalRequest"] = WithdrawalRequest.objects.filter(
                user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["Deposit"] = Deposit.objects.filter(
                user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["TradingAccount"] = TradingAccount.objects.filter(
                user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["Wallet"] = Wallet.objects.filter(
                user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["ReferralAttribution"] = ReferralAttribution.objects.filter(
                referred_user__username__startswith=QA_MARKER,
            ).delete()[0]
            counts["User"] = User.objects.filter(
                username__startswith=QA_MARKER,
            ).delete()[0]

        self.stdout.write(self.style.SUCCESS("IB-PORTAL-UX-10A visual QA cleanup complete."))
        for label, n in counts.items():
            self.stdout.write(f"  {label:45s}: {n}")

    # ─────────────────────────────────────────────────────────────────
    # SEED
    # ─────────────────────────────────────────────────────────────────

    def _seed(self, target_user):
        from simulator.ib_admin_ops import change_commission_rate
        from simulator.ib_commission_triggers import sweep_per_lot
        from simulator.ib_risk_holds import hold_obligation
        from simulator.ib_treasury_settlement import approve_obligation
        from simulator.models import (
            Deposit, IBCommissionObligation, IBCommissionRule, LotExecutionEvent,
            Referral, ReferralAttribution, TradingAccount, WalletTransaction, WithdrawalRequest,
        )
        from simulator.wallet_ledger import credit_wallet, debit_wallet, get_or_create_wallet

        if User.objects.filter(username__startswith=QA_MARKER).exists():
            raise CommandError(
                "QA data already exists (usernames starting with "
                f"{QA_MARKER!r} found). Run with --cleanup first before re-seeding."
            )

        now = timezone.now()

        with transaction.atomic():
            # ── Reuse the IB's REAL Referral — same get_or_create() call
            # associates_view() itself makes; never a second/duplicate
            # Referral for this user. ──
            ref, _ = Referral.objects.get_or_create(
                user=target_user, defaults={"code": f"seed{target_user.pk}"},
            )
            clicks_added = random.randint(20, 30)
            Referral.objects.filter(pk=ref.pk).update(clicks=F("clicks") + clicks_added)
            ref.refresh_from_db()

            # ── QA reviewer — the only permission-bearing actor this
            # command creates, used solely to call the real, unmodified
            # rate/hold/approve services below (none of which move money
            # or touch Treasury). Deleted on --cleanup like everything
            # else. The exact clicks_added delta is stashed in its
            # last_name field (CLICKS_STASH_PREFIX) so --cleanup can
            # precisely decrement Referral.clicks back down — clicks is
            # the ONLY pre-existing, real object this command mutates
            # (Referral.clicks is a single counter with no per-click row
            # to create/delete instead — see the FASE 1 audit), so it
            # needs its own explicit, exact reversal, unlike every other
            # QA object below which is simply deleted. ──
            reviewer = User.objects.create_user(
                username=QA_REVIEWER_USERNAME, password="qa10a-unused", is_staff=True,
                last_name=f"{CLICKS_STASH_PREFIX}{clicks_added}",
            )
            reviewer.user_permissions.add(
                Permission.objects.get(codename="can_review_treasury_request"),
            )
            fake_request = RequestFactory().post("/")
            fake_request.user = reviewer

            # ── 12 QA traders, attributed to the target IB via the real
            # ReferralAttribution mechanism, spread across the last 30
            # days. ──
            traders = []
            for i in range(1, 13):
                trader = User.objects.create_user(
                    username=f"{QA_MARKER}trader_{i:02d}", password="qa10a-unused",
                )
                attributed_at = now - timezone.timedelta(days=random.randint(0, 29), hours=random.randint(0, 23))
                attribution = ReferralAttribution.objects.create(
                    referred_user=trader, referral=ref, source=ReferralAttribution.SOURCE_SESSION,
                )
                ReferralAttribution.objects.filter(pk=attribution.pk).update(attributed_at=attributed_at)
                traders.append(trader)

            # ── 8 Demo accounts (traders 1-8) ──
            for trader in traders[:8]:
                created_at = now - timezone.timedelta(days=random.randint(0, 29))
                acc = TradingAccount.objects.create(
                    user=trader, account_type="DEMO", balance=Decimal("10000.00"),
                )
                TradingAccount.objects.filter(pk=acc.pk).update(created_at=created_at)

            # ── 5 Live accounts (traders 4-8, overlapping with Demo —
            # realistic: a trader can hold both) — exclusively
            # TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES, per the Owner's
            # authorized Live-Accounts decision (IB-PORTAL-UX-10A). ──
            live_accounts = []
            for trader in traders[3:8]:
                created_at = now - timezone.timedelta(days=random.randint(0, 29))
                acc = TradingAccount.objects.create(
                    user=trader, account_type="RETAIL", balance=Decimal("5000.00"),
                )
                TradingAccount.objects.filter(pk=acc.pk).update(created_at=created_at)
                assert acc.account_type in TradingAccount.WITHDRAWABLE_ACCOUNT_TYPES
                live_accounts.append(acc)

            # ── 3 First-Time Deposits (traders 1, 2, 3) — real Deposit
            # row + the matching real credit_wallet() call, so
            # Wallet.available_balance/WalletTransaction stay fully
            # consistent (never just flipping .credited=True with no
            # matching wallet effect). ──
            deposit_amounts = [Decimal("250.00"), Decimal("500.00"), Decimal("750.00")]
            ftd_traders = traders[:3]
            deposits_created = []
            for trader, amount in zip(ftd_traders, deposit_amounts):
                credited_at = now - timezone.timedelta(days=random.randint(0, 29))
                deposit = Deposit.objects.create(
                    user=trader, amount_usd=amount, crypto_currency="USDTTRC20",
                    status=Deposit.STATUS_FINISHED, credited=True, credited_at=credited_at,
                )
                wallet, _ = get_or_create_wallet(trader)
                credit_wallet(
                    wallet.id, amount, WalletTransaction.TX_DEPOSIT, deposit=deposit,
                    note="IB-PORTAL-UX-10A visual QA seed",
                )
                deposits_created.append(deposit)

            # ── 1 completed withdrawal — real debit_wallet() call first
            # (respects the model's own documented invariant: "wallet
            # debit happens ATOMICALLY with the creation of this row"),
            # from the trader with the largest deposit (trader 3, $750). ──
            wd_trader = ftd_traders[2]
            wd_wallet, _ = get_or_create_wallet(wd_trader)
            wd_amount = Decimal("100.00")
            debit_tx = debit_wallet(
                wd_wallet.id, wd_amount, WalletTransaction.TX_WITHDRAW,
                note="IB-PORTAL-UX-10A visual QA seed",
            )
            WithdrawalRequest.objects.create(
                user=wd_trader, amount_usd=wd_amount, crypto_currency="USDTTRC20",
                wallet_address="TQAseedVisualOnlyDoNotUse00000000000",
                status=WithdrawalRequest.STATUS_COMPLETED, debit_tx=debit_tx,
            )

            # ── Real PER_LOT commission rule for this IB, via the real,
            # unmodified change_commission_rate() service — the same
            # mechanism the portal's own "Change Commission Rate" admin
            # action uses. No global rule existed at audit time. Created
            # BEFORE the lot events below: resolve_applicable_rule()
            # correctly (and honestly) refuses to apply a rule
            # retroactively to an event that predates the rule's own
            # effective_from — change_commission_rate() always stamps
            # effective_from=now() and does not accept a backdated value,
            # so the lot events must be timestamped AFTER this call, not
            # spread across the same 30-day window as the acquisition
            # data above. ──
            change_commission_rate(
                ref, Decimal("8.00"), request=fake_request, rule_type=IBCommissionRule.RULE_PER_LOT,
            )
            rule_effective_from = timezone.now()

            # ── Lot execution events on the 5 Live accounts — the
            # certified anchor row itself, created directly (the same
            # shape simulator/tests/test_ib_portal_08b.py's own
            # _make_lot_event() fixture already uses), NOT via the live
            # WS trading engine (consumers.py is protected and out of
            # scope for a management command). Timestamped a few minutes
            # AFTER the rule above (never before it — see the rule-
            # timing note above — and never in the future relative to
            # when this command actually runs) so every event falls
            # inside the rule's real effective window. The Performance
            # Overview chart never reads lot/trade data at all (its 4
            # tabs are Registered/Demo/Live/Deposits, already spread
            # across 30 days above), so this doesn't affect that curve —
            # it only affects the Traded Volume card total and
            # obligation generation, neither of which needs a 30-day
            # spread. ──
            lot_events = []
            for acc in live_accounts:
                for _ in range(random.randint(2, 4)):
                    created_at = rule_effective_from + timezone.timedelta(
                        seconds=random.randint(1, 120),
                    )
                    qty = Decimal(str(round(random.uniform(0.05, 0.40), 2)))
                    ev = LotExecutionEvent.objects.create(
                        account=acc, position=None, symbol="EUR/USD", side=random.choice(["BUY", "SELL"]),
                        qty=qty, execution_price=Decimal("1.10000"),
                        merged=False, entry_path=LotExecutionEvent.ENTRY_MANUAL_WS,
                    )
                    LotExecutionEvent.objects.filter(pk=ev.pk).update(created_at=created_at)
                    lot_events.append(ev)

            # ── Real sweep — the exact, unmodified
            # IB-COMMISSION-TRIGGERS-02A mechanism already used in
            # production (Celery beat), generating real PENDING
            # obligations from the lot events above. Never a second
            # commission calculation. ──
            cutoff = rule_effective_from - timezone.timedelta(minutes=1)
            sweep_result = sweep_per_lot(cutoff)

            # ── Real state variety for the Commission Overview cards —
            # one obligation approved, one held, via the real,
            # unmodified services (neither touches Treasury/Wallet). ──
            qa_obligations = list(
                IBCommissionObligation.objects.filter(
                    attribution__referred_user__username__startswith=QA_MARKER,
                ).order_by("id")
            )
            if len(qa_obligations) >= 1:
                approve_obligation(qa_obligations[0], request=fake_request)
            if len(qa_obligations) >= 2:
                hold_obligation(qa_obligations[1], "IB-PORTAL-UX-10A visual QA", request=fake_request)

        # ── Summary ──────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS("IB-PORTAL-UX-10A visual QA seed complete."))
        self.stdout.write(f"  Target IB          : {target_user.username} (Referral #{ref.pk}, code={ref.code})")
        self.stdout.write(f"  Clicks added       : {clicks_added} (ref.clicks now {ref.clicks})")
        self.stdout.write(f"  QA traders         : {len(traders)} (usernames {QA_MARKER}trader_01..12)")
        self.stdout.write("  Demo accounts      : 8")
        self.stdout.write(f"  Live accounts      : {len(live_accounts)} (WITHDRAWABLE_ACCOUNT_TYPES)")
        self.stdout.write(f"  Deposits (FTD)     : {len(deposits_created)} — ${sum(deposit_amounts)}")
        self.stdout.write(f"  Withdrawals        : 1 — ${wd_amount}")
        self.stdout.write(f"  Lot execution evts : {len(lot_events)}")
        self.stdout.write(f"  sweep_per_lot()    : {sweep_result}")
        self.stdout.write(f"  IB obligations     : {len(qa_obligations)} (1 approved, {'1 held' if len(qa_obligations) >= 2 else '0 held'}, rest pending)")
        self.stdout.write("")
        self.stdout.write(f"  Cleanup: python manage.py seed_ib_portal_visual_qa --user {target_user.username} --cleanup")
