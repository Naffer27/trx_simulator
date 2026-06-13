# Resume: trading-accounts-audit-v1

## Estado al cerrar sesión
- Branch: main
- HEAD: 8390bd3
- Tag: money-flow-audit-v1
- Tests: 1280 pass, 2 skipped, 0 failures

## Milestones completados (orden cronológico)
1. staff-ops-dashboard-v1
2. staff-ops-dashboard-ui-v1
3. withdrawal-2fa-form-fix-v1
4. onboarding-withdrawal-readiness-ux-v1
5. money-flow-audit-v1

## Zonas ya auditadas y blindadas — NO TOCAR
- wallet_ledger.py: credit/debit atomic, InsufficientFunds, reconcile
- deposit callback: idempotencia, firma, pending_balance, doble confirming fix
- withdraw_payout_callback: terminal status guard, doble FAILED fix
- admin approve_withdrawals: pre-claim PENDING→APPROVED antes de API call
- admin reject_withdrawals: atomic con select_for_update
- 2FA gate en withdraw_view
- Readiness UX (home, withdraw, sidebar)

## Próximo bloque: trading-accounts-audit-v1

### Objetivo
Auditar cuentas de trading, trades, PnL, equity, balance, margin, drawdown,
apertura/cierre de operaciones y reglas base — antes de tocar challenges.

### Archivos clave a revisar
- simulator/models.py — TradingAccount, Trade, Position, LedgerEntry
- simulator/views.py — api_orden (apertura/cierre), trading_dashboard
- simulator/risk_engine.py (o similar) — margin guard
- simulator/tests/test_trading*.py — cobertura existente

### Preguntas a responder
1. ¿Puede TradingAccount.balance ir negativo?
2. ¿equity se actualiza correctamente al cerrar posiciones?
3. ¿LedgerEntry se crea en cada trade realizado/cerrado?
4. ¿margin_used se recalcula atómicamente al abrir posición?
5. ¿drawdown diario se calcula correctamente desde initial_balance?
6. ¿Se puede abrir posición sin balance suficiente?
7. ¿Hay guard contra doble cierre de la misma posición?
8. ¿peak_balance se actualiza correctamente?

### Protocolo al retomar
1. git status — confirmar repo limpio en money-flow-audit-v1
2. Leer este archivo
3. Mostrar plan detallado para trading-accounts-audit-v1
4. NO hacer cambios hasta confirmación del usuario
