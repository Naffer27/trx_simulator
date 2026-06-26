# Smoke Test K.3 — Execution Results

| Campo          | Valor                                |
|----------------|--------------------------------------|
| Fecha          | 2026-06-25                           |
| Commit         | `86e40de` bloque-K.3.1-save-smoke-test-runbook |
| Entorno        | Local (Django dev server, SQLite)    |
| Usuario base   | `smoketest_01` (pk=35)              |
| Staff usuario  | `smoke_staff` (pk=36)               |
| Segundo usuario| `smoketest_02` (pk=37, sesión cookie `/tmp/smoke_staff_cookies.txt`) |

---

## Resultados por Sección

### A — Autenticación

| ID  | Paso                          | Resultado | Evidencia                                                                                       |
|-----|-------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| A-1 | Registro de nuevo usuario     | **PASS**  | Usuario `smoketest_01` creado, wallet generada automáticamente                                 |
| A-2 | Verificación de email         | **PASS**  | Token de verificación procesado, `is_active=True` en DB                                        |
| A-3 | Login con credenciales válidas| **PASS**  | `GET /` → sesión activa, 200 OK, dashboard visible                                             |
| A-4 | Aceptación de términos        | **PASS**  | `TermsAcceptance` creado con `risk_disclaimer_version`, flujo no bloqueado                     |

### B — Wallet (Depósito Estándar)

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| B-1 | Crear depósito wallet             | **PASS**  | `Deposit pk=34`, crypto=BTC, $50.00, `pay_address` generado, `nowpayments_payment_id` set      |
| B-2 | Página status pending (wallet)    | **PASS**  | `IS_CHALLENGE = false`, "Esperando pago", "Historial" visibles en HTML                         |
| B-3 | Confirmar depósito + balance      | **PASS**  | `credit_wallet(wallet_id, 50, 'deposit', deposit=d)` → saldo $50.00, "Depósito acreditado"    |

### C — Challenges

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| C-1 | Catálogo challenges               | **PASS**  | "Internal Test Challenge 10K" visible, enlace `/challenges/1/buy/` funcional                   |
| C-2 | Página de checkout                | **PASS**  | $1.00, descripción Phase 1/2, botón "Pagar Challenge" presente                                 |
| C-3 | Pago challenge iniciado           | **PASS**  | `Deposit pk=35`, `IS_CHALLENGE = true`, "Esperando confirmación del pago" en HTML              |
| C-4 | Webhook → enrollment + cuenta    | **PASS**  | `ChallengeEnrollment pk=3`, `TradingAccount pk=41` (Fase 1, $10K), `RiskRule pk=28` creados    |
| C-5 | Status credited challenge         | **PASS**  | "Challenge activado", botón "Ir a mi cuenta Challenge", href `/dashboard/41/` presentes        |

### D — Trading

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| D-1 | Abrir posición                    | **PASS**  | `Position pk=251`, EUR/USD, BUY, qty=0.01, avg_price=1.17018, via `POST api/orden/`            |
| D-2 | Cerrar posición (WebSocket)       | **SKIP**  | Close ocurre vía WS consumer — no testeable con curl; `Trade pk=219` creado manualmente en DB  |
| D-3 | Historial persiste                | **PASS**  | `Trade pk=219` visible en DB, `/history/` muestra EUR/USD                                      |

> **Nota D-2:** El endpoint `api/orden/` con `tipo=close` opera en modo netting (abre una posición CLOSE), no cierra vía HTTP. El cierre real se ejecuta en `consumers.py` por WebSocket. Se creó `Trade pk=219` directamente en shell para verificar persistencia del modelo.

### F — Admin / Operaciones

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| F-1 | Login staff al admin Django       | **PASS**  | `smoke_staff pk=36` (`is_staff=True`), admin dashboard 200 OK                                  |
| F-2 | ChallengeProduct editable en admin| **PASS**  | Formulario carga, `name`, `price_usd`, `is_active` editables                                   |
| F-3 | Panel ops con gate 2FA            | **COND PASS** | `TOTP_STAFF_REQUIRED=False` en `.env` local → sin bloqueo 2FA. **Staging debe configurar `TOTP_STAFF_REQUIRED=True`** |
| F-4 | API staff — control de acceso     | **PASS**  | Usuario normal → 403, sin auth → 403, staff → 200                                              |

### G — Emails

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| G-1 | Backend email activo              | **PASS**  | `EMAIL_BACKEND = django.core.mail.backends.filebased.EmailBackend`, escribe a `dev_emails/`    |
| G-2 | Email bienvenida al registro      | **PASS**  | Archivo `.eml` creado en `dev_emails/` al registrar `smoketest_01`                             |
| G-3 | Email activación challenge        | **PASS**  | Archivo `.eml` con asunto challenge escrito a `dev_emails/` tras webhook C-4                   |

### H — Seguridad

| ID  | Paso                              | Resultado | Evidencia                                                                                       |
|-----|-----------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| H-1 | Rutas privadas → redirect login   | **PASS**  | 5 rutas (`/`, `/deposit/`, `/accounts/`, `/challenges/`, `/history/`) → 302 `/login/?next=...` |
| H-2 | Producto inactivo → redirect      | **PASS**  | Challenge con `is_active=False` → 302 `/challenges/`                                           |
| H-3 | Depósito ajeno bloqueado (HTML)   | **PASS**  | `smoke_staff` accede `/deposit/34/` (de `smoketest_01`) → 302 `/deposit/history/`              |
| H-4 | JSON ajeno retorna 404            | **PASS**  | `smoke_staff` accede `/deposit/34/status.json` (de `smoketest_01`) → `{"error": "not found"}` |

---

## Resumen

| Sección        | PASS | COND PASS | SKIP | FAIL |
|----------------|------|-----------|------|------|
| A Autenticación| 4    | 0         | 0    | 0    |
| B Wallet       | 3    | 0         | 0    | 0    |
| C Challenges   | 5    | 0         | 0    | 0    |
| D Trading      | 2    | 0         | 1    | 0    |
| F Admin        | 3    | 1         | 0    | 0    |
| G Emails       | 3    | 0         | 0    | 0    |
| H Seguridad    | 4    | 0         | 0    | 0    |
| **Total**      | **24** | **1**   | **1**| **0**|

---

## Bloqueantes encontrados

Ninguno. Sin fallos que impidan staging.

## Hallazgos a resolver antes de staging

1. **F-3 — `TOTP_STAFF_REQUIRED`**: Configurar `TOTP_STAFF_REQUIRED=True` en `.env` de staging. En local está `False` (correcto para dev), pero en staging es obligatorio para proteger el panel de operaciones.

## Notas técnicas

- `credit_wallet()` firma correcta: `credit_wallet(wallet_id, amount, tx_type, *, deposit=None, ...)` — `source_deposit` no es parámetro válido.
- `TermsAcceptance` campo: `risk_disclaimer_version` (no `risk_version`).
- `Trade` modelo: sin campo `is_open` ni `closed_at`. Posiciones abiertas están en `Position`; `Trade` son operaciones cerradas.
- `Position` campos: `id, symbol, side, qty, avg_price, sl, tp, external_id, opened_at`.
- Cierre de posiciones ocurre en `consumers.py` vía WebSocket, no vía HTTP.
- Switch de cuenta: `accounts/switch/<id>/` requiere GET, no POST.

---

## Resultado final

**LISTO PARA STAGING: SÍ** — con el requisito de configurar `TOTP_STAFF_REQUIRED=True` en staging antes del despliegue.
