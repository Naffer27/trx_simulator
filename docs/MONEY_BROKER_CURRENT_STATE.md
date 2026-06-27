# Money Broker — Current State
**Checkpoint:** pre-treasury-v1
**Date:** 2026-06-26
**Branch:** main
**Tag:** money-broker-pre-treasury-checkpoint-v1

---

## A. Estado general

- Money Broker está en estado **MVP/beta listo para staging**.
- Full test suite pasa: **1734 tests OK, 2 skipped** (último run completo).
- Admin organizado por módulos con MoneyBrokerAdminSite.
- Challenges visibles e inline desde `/accounts/open/`.
- Challenges se pueden comprar con wallet interna (balance interno).
- NOWPayments sigue disponible como fallback externo de pago crypto.
- Webhooks externos no fueron tocados ni rotos.
- No hay migraciones pendientes.

---

## B. Últimos bloques completados

### ADMIN_UI.1 — Organized Django Admin Sidebar
- Admin reorganizado en 7 secciones: CORE OPERATIONS, TRADING ENGINE, FUNDING PROGRAMS, PAYMENTS & LEDGER, BROKER BUSINESS, GROWTH, TOOLS.
- Implementado via `admin.site.__class__` swap con `MoneyBrokerAdminSite`.
- Sin cambios a registraciones ni URLs de admin.

### UX_CHALLENGES.1 — Simplify Challenge Purchase Flow
- "Capital virtual" → "Capital de evaluación" en todas las pantallas cliente.
- "NOWPayments" removido de textos visibles al cliente (3 instancias en deposit_status, checkout steps).
- Catálogo de challenges reorganizado por tier (10K / 25K / 50K / 100K) usando `{% regroup %}`.
- Orden en `/accounts/open/`: Demo → Real → Fondeo (corregido luego en UX_ACCOUNTS.1).

### UX_ACCOUNTS.1 — Direct Account Opening Layout
- `/accounts/open/` es la pantalla principal de apertura de cuentas.
- Orden definitivo: **Cuentas Reales → Programas de Fondeo → Cuentas Demo**.
- Challenge cards inline con precio, reglas Phase 1/Phase 2, profit split, CTA directo a checkout.
- Link permanente "Ver todos →" a `/challenges/` en header de sección.
- Empty-state con "Empezar Challenge" cuando no hay products activos.
- Vista pasa `challenge_products` queryset al template (read-only, sin migración).

### WALLET_CHALLENGES.1 — Buy Challenges With Internal Wallet Balance
- Usuario puede comprar `ChallengeProduct` usando su balance interno (wallet).
- Flujo si saldo suficiente: `debit_wallet` → `ChallengeEnrollment(deposit=None)` → `activate_challenge_enrollment` → `TradingAccount` + `RiskRule`.
- Flujo si saldo insuficiente: no debita, no activa, redirige a depositar con mensaje claro.
- Protecciones: `transaction.atomic()`, `select_for_update()`, idempotencia (enrollment activo bloquea recompra), rollback total si activation falla.
- `tx_type="CHALLENGE_FEE"` en `WalletTransaction` (sin migración — campo no tiene `CheckConstraint` en DB).
- NOWPayments flow externo completamente intacto.
- 42 tests nuevos en `test_challenge_wallet_purchase.py`.

### K.5 — Account-Type-Aware Trading Sidebar
- Dashboard sidebar split en modo DEMO ("Demo Account / Practice environment") y REAL ("Real Account / Live trading account").
- Template-only usando `acct_rules.account_type`.

---

## C. Flujo actual de dinero en Money Broker

### Depósito externo (crypto via NOWPayments)
```
Usuario deposita crypto
→ Deposit(status=PENDING, challenge_product=None)
→ NOWPayments crea invoice → redirect a deposit_status
→ webhook IPN confirma pago
→ Deposit(status=FINISHED, credited=True)
→ credit_wallet(TX_DEPOSIT) → Wallet.available_balance sube
```

### Depósito de challenge externo (crypto via NOWPayments)
```
Usuario elige challenge en /challenges/<id>/buy/
→ Deposit(status=PENDING, challenge_product=product)
→ NOWPayments crea invoice → redirect a deposit_status
→ webhook IPN confirma pago
→ _fulfill_challenge_purchase(deposit):
    ChallengeEnrollment.create(deposit=deposit)
    activate_challenge_enrollment → TradingAccount + RiskRule
```

### Compra challenge con wallet interna (nuevo — WALLET_CHALLENGES.1)
```
Usuario tiene balance en wallet
→ POST /challenges/<id>/wallet-buy/
→ transaction.atomic():
    debit_wallet(CHALLENGE_FEE) → Wallet.available_balance baja
    ChallengeEnrollment.create(deposit=None)
    activate_challenge_enrollment → TradingAccount + RiskRule
→ redirect /accounts/
```

### Apertura cuenta real con wallet
```
Usuario tiene balance en wallet
→ POST /accounts/create/ con product_id + amount
→ transaction.atomic():
    TradingAccount.create(initial_balance=0)
    transfer_to_account(wallet → account):
        debit_wallet(TX_TRANSFER_OUT)
        LedgerEntry(EV_DEPOSIT) en TradingAccount
        InternalTransfer(COMPLETED)
→ redirect /accounts/
```

---

## D. Pendientes importantes

- **Treasury Engine:** crear sistema separado de treasury. Money Broker wallet actual queda como legacy temporal. Conectar via API cuando Treasury esté probado.
- **NO apagar NOWPayments ni wallet actual** hasta que Treasury esté probado en staging.
- **Product.program_type:** añadir campo `program_type` (Phase1/Phase2/Funded) a `ChallengeProduct` en bloque futuro con migración controlada. Hoy solo se puede agrupar por `tier`.
- **WalletTransaction.TX_CHOICES:** añadir `"CHALLENGE_FEE"` formalmente en cleanup migration (sin schema change — solo choices update).
- **Admin visibility:** mejorar visibilidad de challenge purchases wallet vs crypto en admin.
- **Staging VPS:** pendiente deploy en VPS real.
- **Compliance/product modes:** revisar antes de habilitar real trading real.
- **K.5 sidebar:** tests de dashboard panel ya verdes; revisar con usuarios reales.

---

## E. Cómo retomar después

```bash
cd /Users/naffermoreno/Desktop/trx_sim
git checkout main
git pull origin main
git status
python manage.py check
python manage.py test
```

Luego leer este documento:

```
docs/MONEY_BROKER_CURRENT_STATE.md
```

Para ver el tag de checkpoint:

```bash
git tag | grep pre-treasury
git show money-broker-pre-treasury-checkpoint-v1 --no-patch
```

---

## F. Archivos clave por módulo

| Módulo | Archivos principales |
|--------|---------------------|
| Wallet / Ledger | `simulator/wallet_ledger.py`, `simulator/models.py` (Wallet, WalletTransaction) |
| Challenge purchase | `simulator/views.py` (`challenge_purchase_view`, `challenge_wallet_purchase_view`, `_fulfill_challenge_purchase`) |
| Challenge activation | `simulator/challenge_engine.py` (`activate_challenge_enrollment`) |
| Challenge enrollment | `simulator/models.py` (ChallengeEnrollment, ChallengeProduct) |
| Account opening | `simulator/views.py` (`account_open_view`, `create_account_view`) |
| Admin | `simulator/admin.py` (MoneyBrokerAdminSite) |
| Templates UI | `simulator/templates/simulator/` |
| Tests wallet challenge | `simulator/tests/test_challenge_wallet_purchase.py` |
| Tests account flow | `simulator/tests/test_account_products_flow.py` |
| Tests challenge purchase | `simulator/tests/test_challenge_purchase.py` |

---

*Generado en checkpoint money-broker-pre-treasury-checkpoint-v1 — 2026-06-26*
