# simulator/ib_reversal_admin.py
"""
IB-REVERSALS-FRAUD-05C — admin/operational layer for the reversal
accounting foundation IB-REVERSALS-FRAUD-05B already built.

Approved design: IB-REVERSALS-FRAUD-05C audit + Owner-authorized
minimal scope (no HOLD/FROZEN, no new status, no migration, no fraud
detection). This module makes 05B's four services usable by staff
through Django admin — it adds ZERO financial logic of its own.

Structurally identical to simulator/ib_admin_ops.py (IB-ADMIN-OPS-04B)
and simulator/admin.py::TreasuryOperationRequestAdmin: a fully
read-only-for-manual-CRUD ModelAdmin whose get_urls() attaches custom
staff views, each rendering a template under simulator/templates/admin/
and calling an authoritative service function from
simulator/ib_commission_reversal.py — never reimplementing it.

Money-movement invariant (locked, unchanged from 05B, verified again
here structurally): every view in this file only ever calls into
simulator.ib_commission_reversal's four services. None of them, and
nothing in this file, ever constructs a WalletTransaction, LedgerEntry,
or BrokerLedger directly, ever mutates Wallet.available_balance
directly, or ever calls Treasury's own execute/approve service
functions itself — Treasury execution/approval remain exclusively
Treasury's own authority (simulator/admin.py's existing
TreasuryOperationRequestAdmin), never duplicated here. The only path to
an actual wallet debit is: this admin -> ib_commission_reversal.py's
services -> a TreasuryOperationRequest -> Treasury's own existing,
unmodified execution engine -> wallet_ledger.debit_wallet().

The original IBCommissionObligation and its economic snapshot
(calculated_amount, applied_fixed_rate, applied_percentage_rate,
status, credited_at, treasury_operation) are never written by anything
in this file — only read, to compute remaining_reversible() for
display and to pass through to submit_adjustment().
"""
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import path, reverse

from .ib_commission_reversal import (
    AdjustmentExceedsRemaining, AdjustmentNotApproved, AdjustmentNotEligible,
    AdjustmentNotPending, approve_adjustment, link_adjustment_to_treasury,
    reject_adjustment, remaining_reversible, submit_adjustment,
    sync_adjustment_from_treasury,
)
from .models import IBCommissionAdjustment, IBCommissionObligation
from .treasury_requests import TREASURY_REVIEW_PERMISSION, TREASURY_SUBMIT_PERMISSION

_RECENT_LIMIT = 10


@admin.register(IBCommissionAdjustment)
class IBCommissionAdjustmentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "obligation", "referral", "adjustment_type", "amount", "status",
        "created_by", "created_at", "treasury_operation",
    )
    list_filter = ("adjustment_type", "status", "created_at")
    search_fields = ("referral__code", "referral__user__username", "obligation__id", "reason")
    readonly_fields = [f.name for f in IBCommissionAdjustment._meta.fields]
    ordering = ("-created_at", "-id")

    # Read-only w.r.t. manual CRUD — every state transition flows
    # exclusively through the authoritative services, via the custom
    # views below. Same discipline as IBCommissionObligationAdmin
    # (IB-ADMIN-OPS-04B) and WalletTransactionAdmin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        if request.user.has_perm("simulator.view_ibcommissionadjustment"):
            return True
        if request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            return True
        if request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            return True
        return super().has_view_permission(request, obj)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """
        Injects Approve / Reject / Link-to-Treasury buttons on an
        adjustment's own detail page — same discipline as
        IBCommissionObligationAdmin.change_view() (04B) and
        TreasuryOperationRequestAdmin.change_view() (Treasury). Read-only
        visibility lookup only; the transitions themselves live entirely
        in the custom views below, which call ib_commission_reversal.py
        (05B) unmodified.
        """
        extra_context = extra_context or {}
        instance = IBCommissionAdjustment.objects.filter(pk=object_id).first()
        if instance is not None and request.user.is_authenticated:
            if (
                instance.status == IBCommissionAdjustment.ST_PENDING
                and request.user.has_perm(TREASURY_REVIEW_PERMISSION)
            ):
                extra_context["show_ib_adjustment_approve_button"] = True
                extra_context["ib_adjustment_approve_url"] = reverse(
                    "admin:ib_adjustment_approve", args=[instance.pk],
                )
                extra_context["show_ib_adjustment_reject_button"] = True
                extra_context["ib_adjustment_reject_url"] = reverse(
                    "admin:ib_adjustment_reject", args=[instance.pk],
                )
            if (
                instance.status == IBCommissionAdjustment.ST_APPROVED
                and instance.treasury_operation_id is None
                and request.user.has_perm(TREASURY_SUBMIT_PERMISSION)
            ):
                extra_context["show_ib_adjustment_link_button"] = True
                extra_context["ib_adjustment_link_url"] = reverse(
                    "admin:ib_adjustment_link_treasury", args=[instance.pk],
                )
            if (
                instance.status == IBCommissionAdjustment.ST_APPROVED
                and instance.treasury_operation_id is not None
            ):
                extra_context["show_ib_adjustment_sync_button"] = True
                extra_context["ib_adjustment_sync_url"] = reverse(
                    "admin:ib_adjustment_sync", args=[instance.pk],
                )
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:obligation_id>/ib-adjustment/submit/",
                self.admin_site.admin_view(self.ib_adjustment_submit_view),
                name="ib_adjustment_submit",
            ),
            path(
                "<int:pk>/ib-adjustment/approve/",
                self.admin_site.admin_view(self.ib_adjustment_approve_view),
                name="ib_adjustment_approve",
            ),
            path(
                "<int:pk>/ib-adjustment/reject/",
                self.admin_site.admin_view(self.ib_adjustment_reject_view),
                name="ib_adjustment_reject",
            ),
            path(
                "<int:pk>/ib-adjustment/link-treasury/",
                self.admin_site.admin_view(self.ib_adjustment_link_treasury_view),
                name="ib_adjustment_link_treasury",
            ),
            path(
                "<int:pk>/ib-adjustment/sync/",
                self.admin_site.admin_view(self.ib_adjustment_sync_view),
                name="ib_adjustment_sync",
            ),
        ]
        return custom + urls

    # ── Submit (Service 1) ──────────────────────────────────────────

    def ib_adjustment_submit_view(self, request, obligation_id):
        """
        GET renders a form (amount + reason) showing the obligation's
        current remaining_reversible(); POST calls the real, unmodified
        submit_adjustment(). Requires TREASURY_SUBMIT_PERMISSION.
        Moves no money — creates a PENDING IBCommissionAdjustment only.
        """
        if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

        obligation = IBCommissionObligation.objects.filter(pk=obligation_id).first()
        if obligation is None:
            raise Http404("IB commission obligation not found.")

        obligation_url = reverse("admin:simulator_ibcommissionobligation_change", args=[obligation.pk])
        remaining = remaining_reversible(obligation)

        if request.method == "POST":
            amount_raw = request.POST.get("amount", "").strip()
            reason = request.POST.get("reason", "").strip()
            adjustment_type = request.POST.get(
                "adjustment_type", IBCommissionAdjustment.TYPE_REVERSAL,
            )
            try:
                from decimal import Decimal, InvalidOperation
                amount = Decimal(amount_raw)
            except (InvalidOperation, TypeError):
                messages.error(request, "⚠ Ingresa un monto válido.")
                return self._render_submit_form(request, obligation, remaining)

            try:
                adjustment = submit_adjustment(
                    obligation, amount=amount, reason=reason,
                    adjustment_type=adjustment_type, request=request,
                )
            except AdjustmentNotEligible as exc:
                messages.error(request, f"⚠ {exc}")
                return redirect(obligation_url)
            except AdjustmentExceedsRemaining as exc:
                messages.error(request, f"⚠ {exc}")
                return self._render_submit_form(request, obligation, remaining)
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return self._render_submit_form(request, obligation, remaining)
            except PermissionDenied:
                raise

            messages.success(request, f"✓ Adjustment #{adjustment.pk} submitted (PENDING).")
            return redirect(reverse("admin:simulator_ibcommissionadjustment_change", args=[adjustment.pk]))

        return self._render_submit_form(request, obligation, remaining)

    def _render_submit_form(self, request, obligation, remaining):
        context = dict(
            self.admin_site.each_context(request),
            title=f"Submit Adjustment — Obligation #{obligation.pk}",
            obligation=obligation,
            remaining_reversible=remaining,
            cancel_url=reverse("admin:simulator_ibcommissionobligation_change", args=[obligation.pk]),
            adjustment_types=IBCommissionAdjustment.ADJUSTMENT_TYPE_CHOICES,
        )
        return render(request, "admin/ib_adjustment_submit.html", context)

    # ── Approve ───────────────────────────────────────────────────

    def ib_adjustment_approve_view(self, request, pk):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = IBCommissionAdjustment.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission adjustment not found.")

        detail_url = reverse("admin:simulator_ibcommissionadjustment_change", args=[instance.pk])

        if instance.status != IBCommissionAdjustment.ST_PENDING:
            messages.warning(request, f"⚠ Adjustment #{instance.pk} is no longer PENDING (status={instance.status}).")
            return redirect(detail_url)

        if request.method == "POST":
            try:
                approve_adjustment(instance, request=request)
            except AdjustmentNotPending:
                messages.warning(request, f"⚠ Adjustment #{instance.pk} is no longer PENDING.")
            except AdjustmentExceedsRemaining as exc:
                messages.error(request, f"⚠ {exc}")
            except PermissionDenied:
                raise
            else:
                messages.success(request, f"✓ Adjustment #{instance.pk} approved.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Approve Adjustment #{instance.pk}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_adjustment_approve.html", context)

    # ── Reject ────────────────────────────────────────────────────

    def ib_adjustment_reject_view(self, request, pk):
        if not request.user.has_perm(TREASURY_REVIEW_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_REVIEW_PERMISSION}")

        instance = IBCommissionAdjustment.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission adjustment not found.")

        detail_url = reverse("admin:simulator_ibcommissionadjustment_change", args=[instance.pk])

        if instance.status != IBCommissionAdjustment.ST_PENDING:
            messages.warning(request, f"⚠ Adjustment #{instance.pk} is no longer PENDING (status={instance.status}).")
            return redirect(detail_url)

        if request.method == "POST":
            reason = request.POST.get("rejection_reason", "")
            try:
                reject_adjustment(instance, reason, request=request)
            except AdjustmentNotPending:
                messages.warning(request, f"⚠ Adjustment #{instance.pk} is no longer PENDING.")
            except ValueError as exc:
                messages.error(request, f"⚠ {exc}")
                return render(request, "admin/ib_adjustment_reject.html", dict(
                    self.admin_site.each_context(request),
                    title=f"Reject Adjustment #{instance.pk}", instance=instance, cancel_url=detail_url,
                ))
            except PermissionDenied:
                raise
            else:
                messages.success(request, f"✓ Adjustment #{instance.pk} rejected.")
            return redirect(detail_url)

        context = dict(
            self.admin_site.each_context(request),
            title=f"Reject Adjustment #{instance.pk}",
            instance=instance,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_adjustment_reject.html", context)

    # ── Link to Treasury ──────────────────────────────────────────

    def ib_adjustment_link_treasury_view(self, request, pk):
        if not request.user.has_perm(TREASURY_SUBMIT_PERMISSION):
            raise PermissionDenied(f"Missing permission: {TREASURY_SUBMIT_PERMISSION}")

        instance = IBCommissionAdjustment.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission adjustment not found.")

        detail_url = reverse("admin:simulator_ibcommissionadjustment_change", args=[instance.pk])

        if instance.treasury_operation_id is None and instance.status != IBCommissionAdjustment.ST_APPROVED:
            messages.warning(request, f"⚠ Adjustment #{instance.pk} is not APPROVED (status={instance.status}).")
            return redirect(detail_url)

        if request.method == "POST":
            try:
                treasury_request = link_adjustment_to_treasury(instance, request=request)
            except AdjustmentNotApproved:
                messages.warning(request, f"⚠ Adjustment #{instance.pk} is not APPROVED.")
                return redirect(detail_url)
            except PermissionDenied:
                raise
            else:
                messages.success(
                    request,
                    f"✓ Adjustment #{instance.pk} linked to Treasury debit request "
                    f"#{treasury_request.pk} (status: {treasury_request.status}).",
                )
                return redirect(reverse("admin:simulator_treasuryoperationrequest_change", args=[treasury_request.pk]))

        context = dict(
            self.admin_site.each_context(request),
            title=f"Link Adjustment #{instance.pk} to Treasury",
            instance=instance,
            already_linked=instance.treasury_operation_id is not None,
            cancel_url=detail_url,
        )
        return render(request, "admin/ib_adjustment_link_treasury.html", context)

    # ── Sync (pure observation) ───────────────────────────────────

    def ib_adjustment_sync_view(self, request, pk):
        """
        POST-only — sync_adjustment_from_treasury() is a pure
        observation (no permission of its own, no money movement), so
        this view requires only that the operator can already view IB
        Ops data, not a Treasury-specific permission. Never calls
        Treasury approve/execute.
        """
        if not self.has_view_permission(request):
            raise PermissionDenied("Missing permission to view IB Ops data.")
        if request.method != "POST":
            raise Http404("This action requires POST.")

        instance = IBCommissionAdjustment.objects.filter(pk=pk).first()
        if instance is None:
            raise Http404("IB commission adjustment not found.")

        detail_url = reverse("admin:simulator_ibcommissionadjustment_change", args=[instance.pk])

        result = sync_adjustment_from_treasury(instance)
        outcome = result["outcome"]

        if outcome == "executed":
            messages.success(request, f"✓ Adjustment #{instance.pk} is now EXECUTED.")
        elif outcome == "treasury_terminal_non_success":
            messages.warning(
                request,
                f"⚠ Linked Treasury request reached a terminal non-success state "
                f"({result['treasury_status']}) — adjustment remains APPROVED, no money moved.",
            )
        elif outcome == "already_executed":
            messages.info(request, "This adjustment is already EXECUTED — nothing to sync.")
        else:
            messages.info(request, f"No change — {outcome.replace('_', ' ')}.")

        return redirect(detail_url)
