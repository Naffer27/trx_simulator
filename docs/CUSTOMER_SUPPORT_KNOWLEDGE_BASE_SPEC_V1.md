# Money Broker — Customer Support Knowledge Base Spec V1

**Status:** Documentation only. No code, no models, no migrations were changed to produce this document.
**Owner block:** DOCUMENTATION BLOCK (this document) — precedes and informs `CUSTOMER-SUPPORT-01D`.
**Scope:** Canonical source-of-truth for the content and behavior of the future Knowledge Base / Instant Answers / Support AI layer, and an accurate record of the human support system it will sit in front of.

---

## 0. How to read this document

- **CONFIRMED** — grounded directly in already-implemented, tested code in this repository (`simulator/models.py`, `simulator/views.py`, `simulator/support_panel_views.py`, `simulator/support_status.py`, prior session blocks).
- **DRAFT** — a reasonable, non-binding first pass at content/behavior, not yet validated against real policy documents or legal/compliance review.
- **POLICY_PENDING** — explicitly unresolved. Nothing in this document invents an answer for a `POLICY_PENDING` item; see §14 for the full register.

Every knowledge item below carries this status individually — a category being mostly CONFIRMED does not mean every intent inside it is.

---

## 1. Current Implemented Foundation

This section documents what already exists in the codebase, accurately, as the base the Knowledge Base sits in front of. **The existing human support system is not replaced by anything described later in this document.**

### CUSTOMER-SUPPORT-01A — Data foundation
- `SupportTicket` extended (additively) with: `assigned_to`, `assigned_at`, `escalated_to_ops`, `escalated_at`, `escalated_by`, `escalation_reason`, `first_response_at`, `closed_at`.
- `STATUS_CHOICES` extended to `OPEN`, `PENDING` (legacy, read-compat only, never a new-ticket default and never a transition target), `PENDING_CUSTOMER`, `PENDING_SUPPORT`, `ESCALATED`, `RESOLVED`, `CLOSED`.
- `SupportMessage` created — one row per thread message, `author`, `author_role` (`OWNER`/`OPS`/`SUPPORT`/`CLIENT`, snapshotted at post time), `body`, `visibility` (`CUSTOMER_VISIBLE`/`INTERNAL`), `created_at`, `edited_at`.
- `SupportAttachment` created (schema only — **no upload/download UI exists yet**, deferred to a future block): `ticket`, `message` (nullable), `uploaded_by`, `file`, `filename`, `content_type`, `size_bytes`, with a `clean()`-enforced invariant that a non-null `message` must belong to the same `ticket` as the attachment.
- Original `SupportTicket.message` is never migrated into `SupportMessage` — it is rendered as a synthetic first thread entry at read time in every surface (Support Panel, customer detail page, widget).

### CUSTOMER-SUPPORT-01B — Dedicated Support Panel
- `/staff/support/` and sub-routes, entirely outside `/admin/`.
- Authorization exclusively via `simulator/permission_levels.py` (`is_owner_root`, `is_ops_admin`, `is_customer_support`) — never `user.is_staff`.
- Queue (filters: unassigned / mine / open / waiting for customer / waiting for support / escalated / urgent / resolved-closed / all), ticket detail, `CUSTOMER_VISIBLE` reply, `INTERNAL` note, assignment (claim/unclaim self-service for Support; assign/reassign/unassign Ops/Owner-only), status transitions, escalation to Ops.
- Centralized status-transition legality in `simulator/support_status.py` — no view mutates `SupportTicket.status` directly.

### CUSTOMER-SUPPORT-01C — Customer thread
- `/support/` (create + list, pre-existing), `/support/tickets/<pk>/` (detail), reply, close, reopen — all customer-facing, all ownership-checked via `get_object_or_404(SupportTicket, pk=pk, user=request.user)` (foreign ticket → 404, never 403).
- Thread query filters `visibility=CUSTOMER_VISIBLE` at the database level — `INTERNAL` rows are never fetched into a customer-facing context, let alone rendered.
- Client-eligible status transitions added to the same centralized service: reply reopens `RESOLVED`/`CLOSED` tickets to `OPEN`; reply on `OPEN`/legacy-`PENDING`/`PENDING_CUSTOMER` moves to `PENDING_SUPPORT`; `ESCALATED` tickets are untouched by any reply (Ops retains ownership); customer close is legal from every state except `ESCALATED`.

### CUSTOMER-SUPPORT-01C.1 — Floating chat widget
- Authenticated-only floating widget, persistent on every page extending `base_app.html` except the two staff panels (`/staff/support/`, `/staff/ops/`).
- Reuses `SupportTicket`/`SupportMessage` and the exact same helpers as 01C (`_build_customer_thread`, `_apply_customer_reply`, `_create_support_ticket`) — not a parallel implementation.
- Client bubbles right, Support bubbles left, per-message timestamps, friendly status labels (§14 of the 01C.1 spec — table reproduced in §12 of this document).
- Deterministic active-ticket selection: most recent non-`CLOSED` ticket owned by the customer; empty state offers quick-start category links (pure client-side convenience, no automation) and a new-ticket form.
- Zero `INTERNAL` leakage (DB-level filter, same as 01C); zero cross-user leakage (same ownership filter as every other customer route).

**This foundation is what the Knowledge Base described below must sit in front of, not replace.** A `GREEN` automated answer, a `YELLOW` account-data lookup, or a `RED` escalation all still ultimately produce or touch a `SupportTicket`/`SupportMessage` through this same architecture.

---

## 2. Support Hierarchy

```
CLIENT
  │
  ▼ (automated, approved answers only — GREEN/YELLOW)
Instant Answer / Knowledge Base
  │
  ▼ (unresolved, RED, or explicitly requested)
Support Agent  (permission_levels.is_customer_support)
  │
  ▼ (sensitive operational case)
OPS  (permission_levels.is_ops_admin)
  │
  ▼ (extraordinary)
Owner  (permission_levels.is_owner_root)
```

**Structural constraints, unchanged and non-negotiable for every future block in this line:**
- Support Agent access is, and must remain, structurally separate from Django Admin (`/staff/support/`, never `/admin/`).
- Support (and any future automated layer standing in front of Support) has **no** financial mutation capability: no wallet credit/debit, no balance adjustment, no payout execution, no withdrawal approval, no Treasury action of any kind.
- Support has no server, source code, secrets, or infrastructure access.
- These constraints apply transitively to Support AI (§13) — an AI that can only do what Support can do inherits the same financial and infrastructure exclusion by construction, not by an extra check that could be forgotten.

---

## 3. Knowledge Base Record Model — Conceptual Only

**No database model is created by this document.** This is the conceptual shape `CUSTOMER-SUPPORT-01D` will translate into an actual (additive) schema.

| Field | Meaning |
|---|---|
| `category` | One of the categories in §4–§11 below. |
| `intent` | Stable identifier, e.g. `withdrawal_minimum`. |
| `customer_question_examples` | 2–4 representative phrasings a real customer might use. |
| `approved_answer` | The exact customer-facing language to return for a `GREEN` intent, or the guidance/next-step for `YELLOW`/`RED`. |
| `risk_level` | `GREEN` / `YELLOW` / `RED` (defined below). |
| `action` | `ANSWER` / `LOOKUP` / `SUPPORT` / `OPS` / `OWNER`. |
| `escalation_target` | Who receives it if `action` is `SUPPORT`/`OPS`/`OWNER`. |
| `requires_account_data` | Boolean — does answering require reading this specific customer's real account/transaction state. |
| `policy_status` | `CONFIRMED` / `DRAFT` / `POLICY_PENDING`. |

### Risk levels
- **GREEN** — deterministic, policy-only answer. No account data needed. Safe to return instantly and identically to every customer.
- **YELLOW** — requires a real, authorized lookup of this specific customer's account/transaction/system state (e.g. "where is my deposit #X") before an answer can be given. Never guessed, never answered from GREEN-style static text.
- **RED** — human/OPS/security escalation required. The automated layer must never attempt to resolve these itself.

### Actions
- **ANSWER** — return the approved static answer (GREEN only).
- **LOOKUP** — query authorized, real account/system data, then answer using only what was actually found (YELLOW).
- **SUPPORT** — create/continue a `SupportTicket` and route to a Support Agent.
- **OPS** — escalate to Ops (mirrors 01B's existing escalation mechanism exactly — a `SupportTicket` field flip + status transition, never a new pathway).
- **OWNER** — extraordinary escalation, Owner Root only.

---

## 4. Withdrawals — Approved V1

Ticket category mapping: `SupportTicket.CATEGORY_WITHDRAWAL` (`withdrawal_issue`).

**Confirmed internal facts (grounded in this session's implemented withdrawal work — `simulator/views.py`, `withdrawal_otp.py`, `verified_wallets.py`, `payout_providers.py`, WITHDRAWAL-SECURITY-EXTENSION-01, WITHDRAWAL-POLICY-CORRECTION-01):**
- Two-step flow: `POST /withdraw/` creates a `WithdrawalEmailOTPChallenge` (no money moves yet); `POST /withdraw/otp/` with the correct code creates the `WithdrawalRequest` and debits the wallet, atomically.
- 2FA (TOTP) is required and verified before a withdrawal challenge can even be created.
- A destination wallet must already be a **verified** `VerifiedWithdrawalWallet` (`ACTIVE` status) — free-text addresses are never accepted at withdrawal time.
- A newly registered/changed wallet enters a cooldown period before it becomes usable for withdrawal.
- **Confirmed business policy (POLICY CORRECTION — supersedes the V1 draft figure):** minimum withdrawal is **USD 20**. Amounts from USD 20 through USD 1,000 follow the normal automated security flow (`required_approvals=1`, auto-submitted to the payout provider on OTP verification). Amounts above USD 1,000 require additional internal approval/review before submission (`required_approvals=2`).
- KYC must be `APPROVED` before any withdrawal.
- Wallet ≠ Trading Account: `equity = balance + floating unrealized PnL` on a trading account; the withdrawable balance lives on the customer's **Wallet**, funded by internal transfers from a trading account, never withdrawn directly from trading equity.
- Rejected/failed withdrawals are refunded to the wallet (`TX_CORRECTION`-class ledger entries, already implemented and tested).

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `withdrawal_how_to` | "How do I withdraw?" / "¿Cómo retiro?" | Go to Withdraw, select asset/network, choose a verified wallet, confirm with 2FA, then verify the email code we send you. Funds move only after both steps are complete. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_minimum` | "What's the minimum withdrawal?" | The minimum withdrawal is USD 20. | GREEN | ANSWER | — | No | CONFIRMED (policy). See §14a — a known, separate code-level discrepancy remains open. |
| `withdrawal_kyc_required` | "Do I need KYC to withdraw?" | Yes. KYC approval is required before any withdrawal can be requested. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_processing_time` | "How long does a withdrawal take?" | Withdrawals up to USD 1,000 are processed automatically after verification. Larger withdrawals require an additional internal review step before processing. | GREEN | ANSWER | — | No | CONFIRMED (exact SLA wording — POLICY_PENDING, §14) |
| `withdrawal_processing_status` | "What's the status of my withdrawal?" | Let me check the current status of your withdrawal request. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `withdrawal_not_received` | "I haven't received my withdrawal" | Let me check your withdrawal's real status and, if it was sent, its blockchain confirmation. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | CONFIRMED |
| `withdraw_all` | "Can I withdraw my whole balance?" | Yes — use "Withdraw all" and the full available wallet balance will be requested. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawable_balance_difference` | "Why is my withdrawable balance different from my trading equity?" | Your trading equity (balance + open P&L) is not the same as your wallet balance. Only funds already transferred to your Wallet are withdrawable. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_2fa_required` | "Why do I need 2FA to withdraw?" | Two-factor authentication is required on every withdrawal request to protect your funds. | GREEN | ANSWER | — | No | CONFIRMED |
| `lost_2fa` | "I lost access to my authenticator" | This requires identity-verified account recovery. I'm connecting you with a Support Agent. | RED | SUPPORT | Support Agent | Yes | CONFIRMED (flow), DRAFT (recovery procedure) |
| `withdrawal_email_otp_missing` | "I didn't get the email code" | Check spam/junk first; you can request a new code after the short cooldown. If it still doesn't arrive, I'll flag this for Support. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | CONFIRMED |
| `withdrawal_otp_expired` | "My code expired" | Email codes expire after a short window for your security — request a new one and the previous one is automatically invalidated. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_old_otp_invalid` | "Why doesn't my old code work anymore?" | Only the most recently sent code is valid; requesting a new code invalidates the old one automatically. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_wallet_requirement` | "Why do I need to register a wallet first?" | For security, withdrawals can only go to a wallet address you've previously verified — this prevents funds being sent to an address an attacker just added. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_wallet_cooldown` | "Why can't I use my new wallet right away?" | A newly added or changed wallet has a short security cooldown before it can receive a withdrawal. | GREEN | ANSWER | — | No | CONFIRMED (exact hours — POLICY_PENDING, §14) |
| `withdrawal_change_wallet` | "How do I change my withdrawal wallet?" | Register the new address from the Withdrawal Wallets page; it goes through its own email verification and cooldown before becoming active. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_review_required` | "Why is my withdrawal under review?" | Withdrawals above a certain amount receive an additional internal review before they're sent, as a security measure. | GREEN | ANSWER | — | No | CONFIRMED (do not expose exact internal approval mechanics) |
| `withdrawal_large_amount` | "Can I withdraw a large amount?" | Yes — larger withdrawals simply go through an extra internal review step first. | GREEN | ANSWER | — | No | CONFIRMED |
| `withdrawal_cancel` | "Can I cancel my withdrawal?" | Let me check whether your request has already been sent for processing. | YELLOW | LOOKUP | SUPPORT | Yes | CONFIRMED (checking is possible); cancellation policy itself DRAFT |
| `withdrawal_rejected` | "Why was my withdrawal rejected?" | Let me check the specific reason for your rejected withdrawal — funds are automatically refunded to your wallet in that case. | YELLOW | LOOKUP | SUPPORT if needed | Yes | CONFIRMED |
| `withdrawal_sent_not_received` | "It says sent but I don't see it" | Let me check the transaction hash and its on-chain confirmation status. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | CONFIRMED |
| `withdrawal_txid` | "What's my transaction ID?" | Let me retrieve the transaction hash for your withdrawal. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `withdrawal_received_less` | "I received less than requested" | Let me check the exact amount sent, any network fee, and the transaction record. | YELLOW | LOOKUP | SUPPORT if discrepancy confirmed | Yes | CONFIRMED |
| `withdrawal_unauthorized` | "I didn't request this withdrawal" | This is a security-critical report — connecting you directly with Support now, do not wait. | RED | SUPPORT | Support Agent → OPS | Yes | CONFIRMED |
| `account_takeover_suspected` | "I think someone accessed my account" | This is a security-critical report — connecting you directly with Support now. | RED | SUPPORT | Support Agent → OPS | Yes | CONFIRMED |

**Non-negotiable answer rules for this category:**
- Never claim a withdrawal has "settled" or "arrived" based on elapsed time alone — always require blockchain/provider evidence (transaction hash + confirmation status) before stating funds have arrived.
- Never expose the internal second-approval mechanics (reviewer identity, internal workflow state names) to the customer — only the fact that additional review applies.

---

## 5. Deposits — Approved V1

Ticket category mapping: `SupportTicket.CATEGORY_DEPOSIT` (`deposit_issue`).

**Confirmed internal facts (NowPayments integration, IPN callback, Deposit #45 recovery precedent):**
- Deposits are crypto, via a payment provider (NowPayments) with IPN (webhook) callbacks, signature-verified.
- Deposit lifecycle includes `pending` → `confirming` → `finished` provider-side statuses; the wallet is credited on confirmed completion.
- KYC does **not** currently block deposits.

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `deposit_how_to` | "How do I deposit?" | Go to Deposit, choose your asset, and send funds to the address shown. Your wallet is credited automatically once the network confirms it. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_supported_assets` | "What can I deposit?" | Let me show you the currently supported assets. | YELLOW | LOOKUP | — | No | DRAFT — should be sourced from live config, not hardcoded (§14) |
| `deposit_supported_networks` | "What networks are supported?" | Let me show you the currently supported networks. | YELLOW | LOOKUP | — | No | DRAFT — live config (§14) |
| `deposit_processing_time` | "How long does a deposit take?" | Deposits are credited automatically once the network confirms your transaction — timing depends on network congestion, not on us. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_missing` | "My deposit isn't showing up" | Let me check the real status of your deposit and its blockchain confirmation. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | CONFIRMED |
| `deposit_confirmed_not_credited` | "It shows confirmed on the blockchain but not in my balance" | Let me check the callback/credit status for this specific deposit right now. | YELLOW | LOOKUP | SUPPORT (escalate — known historical failure class) | Yes | CONFIRMED |
| `deposit_wrong_network` | "I sent on the wrong network" | This requires manual review — connecting you with Support now. | RED | SUPPORT | Support Agent | Yes | CONFIRMED |
| `deposit_wrong_address` | "I sent to the wrong address" | This requires manual review — connecting you with Support now. | RED | SUPPORT | Support Agent | Yes | CONFIRMED |
| `deposit_address` | "What's my deposit address?" | Let me show you your current deposit address for that asset/network. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `deposit_address_change` | "Can my deposit address change?" | Deposit addresses can be provider-generated per transaction — always use the address shown for your CURRENT deposit request. | GREEN | ANSWER | — | No | DRAFT |
| `deposit_minimum` | "Is there a minimum deposit?" | Let me confirm the current minimum for your asset. | YELLOW | LOOKUP | — | No | POLICY_PENDING (§14) |
| `deposit_fee` | "Are there deposit fees?" | Let me confirm the current fee policy. | YELLOW | LOOKUP | — | No | POLICY_PENDING (§14) |
| `deposit_wrong_amount` | "I sent a different amount than I intended" | Your wallet is credited based on what was actually received and confirmed on-chain, not the originally intended amount. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_cancel` | "Can I cancel a deposit?" | Crypto deposits can't be cancelled once broadcast on-chain — let me check its current status. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `deposit_without_kyc` | "Can I deposit without KYC?" | Yes — KYC is not required to deposit. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_external_wallet` | "Can I deposit from an external wallet?" | Yes, from any wallet capable of sending on the supported network. | GREEN | ANSWER | — | No | DRAFT |
| `deposit_third_party_wallet` | "Can I deposit from someone else's wallet?" | Let me check our policy on third-party-sourced deposits for your case. | YELLOW | LOOKUP | SUPPORT (AML-sensitive) | Yes | POLICY_PENDING (§14) |
| `deposit_pending` | "My deposit says pending" | Pending means we're waiting for network confirmations — let me check the current count. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `deposit_finished` | "My deposit says finished, what now?" | "Finished" means it's fully confirmed and credited to your wallet. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_history` | "Where can I see my deposit history?" | Your full deposit history is available on the Deposit History page. | GREEN | ANSWER | — | No | CONFIRMED |
| `deposit_txid` | "What's my deposit transaction ID?" | Let me retrieve the transaction hash for your deposit. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `deposit_balance_mismatch` | "My balance doesn't match what I deposited" | Let me check your deposit and wallet ledger in detail. | YELLOW | LOOKUP | SUPPORT if discrepancy confirmed | Yes | CONFIRMED |

**Non-negotiable answer rules for this category:** wrong-network and wrong-address reports always escalate (RED) — never attempt an automated resolution or promise of recovery. Do not hardcode which assets/networks are supported in the static answer text; source from live configuration when `01D` is implemented.

---

## 6. KYC — Draft V1

Ticket category mapping: `SupportTicket.CATEGORY_KYC` (`kyc_issue`).

**Confirmed policy:**
- KYC does **not** block: deposits, demo trading, normal trading, challenge purchases/participation.
- KYC **does** block: withdrawals, future funded-account payouts.

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `kyc_why` | "Why do I need to verify my identity?" | Identity verification (KYC) is required before withdrawing funds, as a regulatory and security measure. | GREEN | ANSWER | — | No | CONFIRMED |
| `kyc_documents` | "What documents do I need?" | Let me confirm the exact accepted document types for your country. | YELLOW | LOOKUP | — | No | POLICY_PENDING (§14) |
| `kyc_processing_time` | "How long does KYC review take?" | Let me check the current status of your submission. | YELLOW | LOOKUP | — | Yes | POLICY_PENDING (exact SLA, §14) |
| `kyc_rejected` | "Why was my KYC rejected?" | Let me check the specific reason for your rejection so you can resubmit correctly. | YELLOW | LOOKUP | SUPPORT if unclear | Yes | CONFIRMED (rejection exists), DRAFT (standard reasons) |
| `kyc_resubmit` | "How do I resubmit my documents?" | You can resubmit from the Verification page after a rejection. | GREEN | ANSWER | — | No | CONFIRMED |
| `kyc_deposit_requirement` | "Do I need KYC to deposit?" | No — KYC is not required to deposit. | GREEN | ANSWER | — | No | CONFIRMED |
| `kyc_trading_requirement` | "Do I need KYC to trade?" | No — KYC is not required for demo or normal trading. | GREEN | ANSWER | — | No | CONFIRMED |
| `kyc_withdrawal_requirement` | "Do I need KYC to withdraw?" | Yes — KYC approval is required before any withdrawal. | GREEN | ANSWER | — | No | CONFIRMED |
| `kyc_data_security` | "How is my ID document data protected?" | Your documents are stored securely and access is restricted to authorized verification staff only. | GREEN | ANSWER | — | No | DRAFT — should be reviewed against actual `secure_media.py` KYC-document authorization matrix before final publication |

---

## 7. Trading — Draft V1

Ticket category mapping: `SupportTicket.CATEGORY_TRADING` (`trading_issue`).

**Confirmed fact:** `equity = balance + floating (unrealized) P&L`. This single relationship underlies several answers below.

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `trading_how_to_open` | "How do I open a trade?" | Select your instrument, choose Buy or Sell, set your lot size, and confirm. | GREEN | ANSWER | — | No | DRAFT |
| `trading_how_to_close` | "How do I close a trade?" | Open your Positions panel and select Close on the trade you want to exit. | GREEN | ANSWER | — | No | DRAFT |
| `trading_buy_sell` | "What's the difference between Buy and Sell?" | Buy profits when price rises; Sell (short) profits when price falls. | GREEN | ANSWER | — | No | DRAFT |
| `trading_lot` | "What is a lot?" | A lot is the standardized trade size unit for the instrument you're trading. | GREEN | ANSWER | — | No | DRAFT |
| `trading_spread` | "What is the spread?" | The spread is the difference between the buy and sell price, and is part of how execution cost is calculated. | GREEN | ANSWER | — | No | CONFIRMED (mechanism exists — `BrokerSpreadConfig`), DRAFT (public wording) |
| `trading_margin` | "What is margin?" | Margin is the portion of your balance reserved to keep a position open. | GREEN | ANSWER | — | No | DRAFT |
| `trading_leverage` | "What is my leverage?" | Let me check the leverage configured for your account. | YELLOW | LOOKUP | — | Yes | CONFIRMED (field exists) |
| `trading_equity` | "What is equity?" | Equity is your balance plus the floating (unrealized) profit or loss of your open positions. | GREEN | ANSWER | — | No | CONFIRMED |
| `trading_balance` | "What is my balance?" | Let me check your current account balance. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `trading_free_margin` | "What is free margin?" | Free margin is your equity minus the margin currently used by open positions. | GREEN | ANSWER | — | No | DRAFT |
| `trading_stop_loss` | "How does stop loss work?" | A stop loss automatically closes your position at a price you set, to limit downside. | GREEN | ANSWER | — | No | DRAFT |
| `trading_take_profit` | "How does take profit work?" | A take profit automatically closes your position at a price you set, to lock in gains. | GREEN | ANSWER | — | No | DRAFT |
| `trading_pending_order` | "What is a pending order?" | A pending order executes automatically once the market reaches a price you specify. | GREEN | ANSWER | — | No | CONFIRMED (feature exists — `PendingOrder`) |
| `trading_insufficient_margin` | "Why can't I open this trade?" | Let me check your available margin for this trade size. | YELLOW | LOOKUP | — | Yes | CONFIRMED |
| `trading_pnl_calculation` | "How is my P&L calculated?" | Let me walk through the exact calculation for your specific position. | YELLOW | LOOKUP | SUPPORT if disputed | Yes | CONFIRMED (mechanism exists) |
| `trading_commission` | "How much commission do I pay?" | Let me check the commission structure for your account/instrument. | YELLOW | LOOKUP | — | Yes | POLICY_PENDING (public rate disclosure, §14) |
| `trading_market_closed` | "Why can't I trade right now?" | Let me check the current market/instrument trading hours. | YELLOW | LOOKUP | — | No | DRAFT |
| `trading_order_rejected` | "Why was my order rejected?" | Let me check the specific reason your order was rejected. | YELLOW | LOOKUP | SUPPORT if unclear | Yes | CONFIRMED |
| `trading_history` | "Where's my trade history?" | Your full trade history is available on the History page. | GREEN | ANSWER | — | No | CONFIRMED |
| `trading_modify_sl_tp` | "Can I change my stop loss/take profit after opening?" | Yes, you can modify SL/TP on an open position at any time. | GREEN | ANSWER | — | No | CONFIRMED (validated update path exists) |
| `trading_chart_execution_mismatch` | "My execution price doesn't match the chart" | This needs a detailed look at your specific execution — connecting you with Support. | RED | SUPPORT | Support Agent | Yes | CONFIRMED |
| `trading_initial_negative_pnl` | "Why is my P&L negative right when I open a trade?" | This is normal — it reflects the spread cost at entry, not a loss on the position itself. | GREEN | ANSWER | — | No | DRAFT |

**Non-negotiable answer rule for this category:** a specific execution dispute (wrong price, wrong side, unexpected slippage, chart-vs-fill mismatch) is **never** answered with a generic explanation. It requires inspection of side, bid/ask, timestamp, execution price, spread, lot size, commission, P&L, and chart source — this makes it `YELLOW` at best and usually `RED`, never `GREEN`.

---

## 8. Account

Ticket category mapping: `SupportTicket.CATEGORY_ACCOUNT` (`account_issue`).

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `login_problems` | "I can't log in" | Let me check your account status. | YELLOW | LOOKUP | SUPPORT if account-level issue | Yes | DRAFT |
| `forgot_password` | "I forgot my password" | Use "Forgot password" on the login page to reset it via email. | GREEN | ANSWER | — | No | CONFIRMED |
| `email_verification` | "How do I verify my email?" | Check your inbox for the verification link, or request a new one from your profile. | GREEN | ANSWER | — | No | CONFIRMED |
| `profile_update` | "How do I update my profile?" | You can update your profile details from the Profile page. | GREEN | ANSWER | — | No | CONFIRMED |
| `account_locked` | "My account is locked" | This needs manual review — connecting you with Support. | RED | SUPPORT | Support Agent | Yes | DRAFT |
| `multiple_accounts` | "Can I have more than one account?" | Let me check our current policy on multiple accounts. | YELLOW | LOOKUP | SUPPORT | No | POLICY_PENDING (§14) |
| `2fa_setup` | "How do I set up 2FA?" | Go to Security settings and follow the 2FA setup steps with your authenticator app. | GREEN | ANSWER | — | No | CONFIRMED |
| `lost_2fa` | (see §4 — same intent, cross-referenced) | — | RED | SUPPORT | Support Agent | Yes | CONFIRMED |
| `account_closure` | "How do I close my account?" | This requires identity-verified confirmation — connecting you with Support. | RED | SUPPORT | Support Agent | Yes | DRAFT |

**Rule:** any security-sensitive account recovery (lost 2FA, suspected takeover, account closure) escalates — never handled as an automated `GREEN`/`YELLOW` flow.

---

## 9. Security

Ticket category mapping: `SupportTicket.CATEGORY_ACCOUNT` (`account_issue`) today — **no dedicated `security` ticket category currently exists**; see §14.

All intents in this category are **RED by definition** — no exceptions, no automated resolution attempt of any kind.

| intent | example questions | approved_answer | risk | action | escalation |
|---|---|---|---|---|---|
| `unauthorized_withdrawal` | "I didn't request this withdrawal" | This is a security-critical report — connecting you with Support immediately. | RED | SUPPORT | Support Agent → OPS |
| `unauthorized_trade` | "I didn't open this trade" | This is a security-critical report — connecting you with Support immediately. | RED | SUPPORT | Support Agent → OPS |
| `account_takeover_suspicion` | "I think my account was compromised" | This is a security-critical report — connecting you with Support immediately. | RED | SUPPORT | Support Agent → OPS |
| `phishing` | "I got a suspicious email/message claiming to be Money Broker" | Thank you for reporting this — connecting you with Support to verify and investigate. | RED | SUPPORT | Support Agent |
| `lost_authenticator` | (cross-reference `lost_2fa`, §4/§8) | — | RED | SUPPORT | Support Agent |
| `unknown_login_activity` | "I see a login I don't recognize" | This is a security-critical report — connecting you with Support immediately. | RED | SUPPORT | Support Agent → OPS |
| `balance_anomaly` | "My balance changed and I don't know why" | Connecting you with Support to investigate this immediately. | RED | SUPPORT | Support Agent → OPS |

**Absolute rule:** the automated assistant must never minimize, reassure prematurely, or attempt to "explain away" a security report. Every one of these routes straight to a human, every time.

---

## 10. Challenges / Funded

Ticket category mapping: `SupportTicket.CATEGORY_CHALLENGE` (`challenge_issue`).

Commercial/product rules for Challenges and Funded accounts are **not finalized** in this document. The topics below establish the intent taxonomy only — **every answer is `POLICY_PENDING`** until confirmed against the actual, current `ChallengeProduct`/`FundedConfig` configuration and business decision.

| intent (topic) | POLICY_PENDING because |
|---|---|
| `challenge_purchase` | purchase flow exists in code; public explanation not drafted |
| `challenge_price` | prices are per-product/configurable — must be sourced live, never hardcoded |
| `challenge_account_size` | configurable per product |
| `challenge_profit_target` | configurable per product/phase |
| `challenge_drawdown` | configurable per product |
| `challenge_daily_loss` | configurable per product |
| `challenge_phases` | phase count/rules per product |
| `challenge_min_trading_days` | configurable per product/phase |
| `challenge_ea_policy` | Expert Advisor allowance not confirmed for public answer |
| `challenge_news_trading` | policy not confirmed |
| `challenge_overnight_holding` | policy not confirmed |
| `challenge_weekend_holding` | policy not confirmed |
| `funded_activation` | activation flow exists (`FundedConfig`) — public explanation not drafted |
| `funded_payout` | payout mechanics exist in code — public SLA/process not drafted |
| `funded_profit_split` | configurable per `FundedConfig.profit_split_pct` — must be sourced live |
| `funded_kyc_requirement` | KYC blocks funded payouts (confirmed, §6) — full funded-specific wording not drafted |

Do not invent final Challenge/Funded commercial policy in `01D`. This category should be the last one populated with `GREEN` answers, after explicit business confirmation.

---

## 11. Technical Support

Ticket category mapping: `SupportTicket.CATEGORY_BUG` (`bug`).

| intent | example questions | approved_answer | risk | action | escalation | acct data | status |
|---|---|---|---|---|---|---|---|
| `page_not_loading` | "The page won't load" | Try refreshing or clearing your browser cache; if it persists, I'll escalate this. | YELLOW | LOOKUP | SUPPORT if persists | No | DRAFT |
| `chart_frozen` | "My chart isn't updating" | Try refreshing the page; if the issue continues, I'll flag it for our technical team. | YELLOW | LOOKUP | SUPPORT if persists | No | DRAFT |
| `market_data_not_updating` | "Prices aren't updating" | Let me check current market data feed status. | YELLOW | LOOKUP | SUPPORT if confirmed outage | No | DRAFT |
| `order_button_not_working` | "The trade button doesn't respond" | Let me check for a known issue; if none is found, I'll escalate for investigation. | YELLOW | LOOKUP | SUPPORT | Yes | DRAFT |
| `cant_open_close_trade` | "I can't open/close a trade" | This can have several causes (margin, market hours, connectivity) — let me check your specific case. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | CONFIRMED (as an escalation path) |
| `balance_display_issue` | "My balance looks wrong on screen" | Let me check your real balance against what's displayed. | YELLOW | LOOKUP | SUPPORT if mismatch confirmed | Yes | CONFIRMED |
| `email_not_received` | "I'm not receiving emails" | Check spam/junk first; let me also confirm delivery on our side. | YELLOW | LOOKUP | SUPPORT if confirmed failure | Yes | DRAFT |
| `document_upload_issue` | "I can't upload my KYC document" | Let me check accepted file types/size, and confirm whether your upload was received. | YELLOW | LOOKUP | SUPPORT if unresolved | Yes | DRAFT |
| `mobile_browser_compatibility` | "The site doesn't work well on my phone/browser" | Let me note your device/browser details for our technical team. | GREEN | ANSWER | SUPPORT (info collection) | No | DRAFT |

**Rule:** any technical report that turns out to have a real financial inconsistency behind it (balance mismatch, P&L discrepancy, missing trade) escalates immediately — technical support intents are a starting classification, not a place to resolve financial disputes.

---

## 12. Future Automated Support Flow

```
Customer opens widget
  │
  ▼
Chooses a quick-start category, or types a free-text question
  │
  ▼
Intent matching (against the knowledge base described in §3–§11)
  │
  ├── GREEN  → return the approved answer instantly (ANSWER)
  │
  ├── YELLOW → query authorized, real account/system data for
  │             THIS customer, then answer using only what was
  │             actually found (LOOKUP) — never guessed
  │
  └── RED    → create or continue a SupportTicket and escalate
               immediately (SUPPORT)
  │
  ▼
Unresolved after an automated attempt
  │
  ▼
Transfer to a Support Agent (existing /staff/support/ queue —
  the exact same ticket, not a new one)
  │
  ▼ (sensitive operational case, per the existing escalation
  │  mechanism already implemented in 01B)
  OPS
  │
  ▼ (extraordinary)
  Owner
```

**Absolute rules, carried over unchanged from every prior block in this line:**
- No fake agent presence ("An agent is now connected" is never shown unless a real human is actually assigned).
- No fake transaction status (never claim something settled/arrived without real evidence).
- No hallucinated policy (an intent with no confirmed answer is `POLICY_PENDING` and is never silently answered anyway).

---

## 13. Future Support AI Rules

**Support AI, initially, is permitted to:**
- Classify an incoming customer message into an intent.
- Retrieve the matching approved knowledge item.
- Summarize a ticket/thread for a human agent.
- Draft a reply (as an `INTERNAL` `SupportMessage`, per the existing 01B Design Lock's own forward-compatibility note — a draft is never customer-visible until a human sends it, or a `GREEN` intent's pre-approved static answer is returned automatically).
- Route/escalate according to the risk-level rules above.

**Support AI must NOT, in this or the next block:**
- Move money in any form.
- Approve withdrawals.
- Alter balances.
- Change KYC status.
- Execute payouts.
- Modify trading accounts.
- Invent policy that isn't in this document as `CONFIRMED` or explicit product configuration.
- Mark any transaction "settled"/"received" without real provider/blockchain evidence.

This list is a direct extension of §2's human-Support exclusions — an AI standing in front of Support inherits the exact same financial and infrastructure boundary, never a wider one.

---

## 14. POLICY_PENDING Register

Every item below is explicitly unresolved. None of them are answered by assumption anywhere in this document.

| # | Item | Why it's pending |
|---|---|---|
| 1 | Exact public withdrawal SLA (processing time wording) | Automated-flow timing exists in code; no confirmed public-facing SLA text. |
| 2 | Exact wallet-change cooldown disclosure (hours) | Cooldown mechanism exists and is enforced; the exact customer-facing number needs confirmation before publishing as `GREEN`. |
| 3 | Deposit minimum rules | No confirmed minimum-deposit policy found. |
| 4 | Deposit fee policy | No confirmed fee-disclosure policy found. |
| 5 | Third-party wallet / deposit AML policy | Needs explicit compliance confirmation before any automated answer. |
| 6 | Exact KYC accepted documents (per country/region) | Not confirmed for public wording. |
| 7 | KYC review SLA | Not confirmed for public wording. |
| 8 | Final Challenge/Funded commercial & trading rules | Entire §10 category — deliberately left unresolved pending business confirmation. |
| 9 | Exact trading commission/spread publication rules | Mechanism exists (`BrokerSpreadConfig`); public disclosure wording not confirmed. |
| 10 | No dedicated `security` `SupportTicket` category exists today | §9's intents currently map to `account_issue` for lack of a better fit — worth a real category addition in a future additive migration, not assumed here. |
| 11 | Deposit supported assets/networks — static vs. live-sourced | Current draft answers are placeholders; §5 explicitly requires these be sourced from live configuration, not hardcoded, when implemented. |

---

## 14a. Known Code/Policy Discrepancy — Withdrawal Minimum (NOT policy-pending, code reconciliation only)

**Policy status: CONFIRMED.** Per the POLICY CORRECTION issued after the V1 draft, the minimum withdrawal is **USD 20**, and this is what §4's `withdrawal_minimum` intent answers with. This is no longer an open policy question.

**What remains open is a code-level reconciliation, not a policy question:** the currently implemented `WithdrawForm.amount_usd` field validation only enforces a floor of USD 0.01 (set in an earlier session block, `WITHDRAWAL-POLICY-CORRECTION-01`, at a time when the confirmed business policy was different). As of this document, the application has **not** been changed to enforce the USD 20 floor — that form-level validation update is explicitly **out of scope for this documentation block** and must happen in a separate, dedicated code-fix block.

**Until that code-fix block runs:** the confirmed public policy (USD 20 minimum) and the actual server-side enforcement (USD 0.01 minimum) are different. The Knowledge Base must state the confirmed policy (USD 20) as the answer regardless — it must never describe the current USD 0.01 code floor as if it were an alternative or valid business policy; it is a known, temporary enforcement gap awaiting a fix, nothing more.

---

## 15. Future Implementation Block

The next expected implementation block in this line is:

**`CUSTOMER-SUPPORT-01D` — Knowledge Base + Instant Answers Foundation**

This document does not implement it. When authorized, `01D`'s guiding principle is:

```
existing widget (01C.1)
  + existing human Support system (01B/01C)
  + deterministic approved GREEN answers (this document, §4–§11)
  + the risk-level/escalation rules (§3, §12, §13)
```

— **not** a replacement architecture. `01D` is expected to be additive: a new, small knowledge-base schema (per §3's conceptual shape) plus an intent-matching layer sitting *in front of* the existing `SupportTicket`/`SupportMessage`/widget/Support-Panel stack, never bypassing or duplicating it.

---

## Appendix — Category → Ticket Category Mapping

| Knowledge Base Category | `SupportTicket.CATEGORY_CHOICES` value |
|---|---|
| Withdrawals | `withdrawal_issue` |
| Deposits | `deposit_issue` |
| KYC | `kyc_issue` |
| Trading | `trading_issue` |
| Account | `account_issue` |
| Security | `account_issue` *(no dedicated category yet — see §14 item 11)* |
| Challenges / Funded | `challenge_issue` |
| Technical Support | `bug` |
| (uncategorized/other) | `other` |
