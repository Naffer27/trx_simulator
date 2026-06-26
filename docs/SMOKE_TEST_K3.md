# Money Broker — Manual Smoke Test Checklist (Bloque K.3)

**Versión:** K.3 · commit `eecd7f2` · 2026-06-25
**Entorno objetivo:** local `DEBUG=True` → staging VPS
**Puerto local:** `http://127.0.0.1:8000`

---

## PREPARACIÓN PREVIA

### Usuario de prueba recomendado

| Campo | Valor sugerido |
|---|---|
| **Email** | `smoketest@moneybroker.local` |
| **Username** | `smoketest_01` |
| **Password** | `SmokeTest2026!` |
| **Rol** | Normal (no staff) |
| **Staff separado** | `admin@moneybroker.local` / `is_staff=True` |

### Setup de entorno antes de empezar

```bash
# 1. Servidor corriendo
python manage.py runserver

# 2. Celery worker (para emails async)
celery -A trx_simulator worker -l info

# 3. Seed del challenge product de prueba
python manage.py seed_test_challenge_product

# 4. Confirmar que existe en admin
# → Admin > Challenge Products > "Internal Test Challenge 10K"
```

### Configuración mínima `.env` para smoke test local

```
DEBUG=True
EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
NOWPAYMENTS_API_KEY=<key real o sandbox>
NOWPAYMENTS_IPN_SECRET=<secret real o placeholder>
TOTP_STAFF_REQUIRED=True
```

---

## SECCIÓN A — AUTH / ONBOARDING

### A-1. Registro

| Paso | Acción | URL |
|---|---|---|
| A-1.1 | Abrir landing | `http://127.0.0.1:8000/` |
| A-1.2 | Ir a registro | `http://127.0.0.1:8000/register/` |
| A-1.3 | Llenar: username=`smoketest_01`, email=`smoketest@…`, password=`SmokeTest2026!` | — |
| A-1.4 | Submit | — |

**Resultado esperado:** Redirect a `/home/` o página de "verifica tu email". En consola del servidor se imprime el email de verificación (si `EMAIL_BACKEND=console`).

**Evidencia:** Screenshot del banner de verificación + línea del email en consola.

**PASS:** Banner visible, email en log.
**FAIL:** Error 500 / formulario no guarda usuario.
**¿Bloqueante?** ✅ SÍ

---

### A-2. Email verification

| Paso | Acción |
|---|---|
| A-2.1 | Copiar el token del log de consola: `verify-email/<token>/` |
| A-2.2 | Abrir `http://127.0.0.1:8000/verify-email/<token>/` |
| A-2.3 | Confirmar que la página dice "Email verificado" |

**Evidencia:** Screenshot de la página de confirmación. En Admin: `EmailVerification.verified = True`.

**PASS:** `EmailVerification.objects.get(user=smoketest_01).verified == True`
**FAIL:** Token inválido / error.
**¿Bloqueante?** ✅ SÍ — sin verificar no se puede crear depósito ni cuenta.

---

### A-3. Login

| Paso | Acción | URL |
|---|---|---|
| A-3.1 | Ir a login | `http://127.0.0.1:8000/login/` |
| A-3.2 | Ingresar credenciales y submit | — |
| A-3.3 | Verificar redirect a `/home/` | — |

**PASS:** Sesión activa, sidebar visible.
**FAIL:** "Credenciales incorrectas" con datos correctos.
**¿Bloqueante?** ✅ SÍ

---

### A-4. Aceptación de Terms

| Paso | Acción | URL |
|---|---|---|
| A-4.1 | Intentar ir a `/deposit/` sin aceptar terms | `http://127.0.0.1:8000/deposit/` |
| A-4.2 | Verificar redirect a `/legal/accept/` | — |
| A-4.3 | Aceptar terms y submit | `http://127.0.0.1:8000/legal/accept/` |

**Resultado esperado en A-4.1:** Redirect automático a `/legal/accept/` (no error 500).
**Resultado esperado en A-4.3:** Redirect de vuelta a `/deposit/`.
**Evidencia:** `TermsAcceptance.objects.filter(user=smoketest_01, terms_version="2026-06-v1")` existe en Admin.

**PASS:** Record `TermsAcceptance` creado.
**FAIL:** No redirige / error al aceptar.
**¿Bloqueante?** ✅ SÍ

---

### A-5. 2FA (opcional para usuario normal)

| Paso | Acción | URL |
|---|---|---|
| A-5.1 | Ir a setup 2FA | `http://127.0.0.1:8000/account/2fa/setup/` |
| A-5.2 | Escanear QR con Authenticator | — |
| A-5.3 | Ingresar código TOTP y confirmar | — |
| A-5.4 | Verificar banner de "2FA activado" | — |

**Evidencia:** `TOTPDevice.objects.get(user=smoketest_01, confirmed=True)` en Admin.

**PASS:** Device confirmado en DB.
**FAIL:** Código siempre inválido.
**¿Bloqueante?** ⚠️ No bloqueante para usuario normal — bloqueante para staff.

---

### A-6. KYC (informacional / necesario para retiros)

| Paso | Acción | URL |
|---|---|---|
| A-6.1 | Ir a KYC | `http://127.0.0.1:8000/kyc/` |
| A-6.2 | Llenar formulario con datos de prueba y submit | — |
| A-6.3 | Verificar status "Pendiente de revisión" | — |
| A-6.4 | En Admin: aprobar KYC manualmente | `http://127.0.0.1:8000/admin/simulator/kycprofile/` |

**Evidencia:** `KYCProfile.status == "approved"` en Admin.
**PASS:** Status cambia a `approved` en Admin.
**FAIL:** Formulario no guarda / error 500.
**¿Bloqueante?** ⚠️ Solo bloqueante para retiros y funded payout.

---

## SECCIÓN B — WALLET / DEPÓSITOS NORMALES

### B-1. Crear depósito wallet

| Paso | Acción | URL |
|---|---|---|
| B-1.1 | Ir a depósito | `http://127.0.0.1:8000/deposit/` |
| B-1.2 | Seleccionar `btc`, monto `$50` | — |
| B-1.3 | Submit "Generar dirección de pago" | — |
| B-1.4 | Verificar redirect a `/deposit/<id>/` | — |

**Resultado esperado:** Página de status con dirección BTC, QR code, banner "Esperando pago".
**Evidencia:** `Deposit` creado en Admin (`challenge_product=None`). URL contiene `/deposit/<id>/`.

**PASS:** Página de status carga, dirección visible.
**FAIL:** Error NOWPayments (`NOWPAYMENTS_API_KEY` no configurado).
**¿Bloqueante?** ✅ SÍ si NOWPayments real. ⚠️ Skipeable si se usa sandbox.

---

### B-2. Status pending y textos wallet

| Paso | Acción |
|---|---|
| B-2.1 | En `/deposit/<id>/` verificar: banner "Esperando pago" |
| B-2.2 | Verificar: sub-texto "Envía exactamente el monto…" |
| B-2.3 | Verificar: info-row "El balance se acredita automáticamente en tu wallet…" |
| B-2.4 | Verificar: NO aparece "Challenge activado" / "Esperando confirmación del pago" |
| B-2.5 | Verificar: links "Historial" y "Nuevo depósito" visibles (NO "Ver Challenges") |

**Evidencia:** Screenshot de la página completa.

---

### B-3. Simular confirmación de depósito wallet

**Método seguro en local (sin webhook real):**
```bash
python manage.py shell
>>> from simulator.models import Deposit, Wallet
>>> from simulator.wallet_ledger import credit_wallet
>>> d = Deposit.objects.get(pk=<id>)
>>> d.status = 'finished'; d.credited = True; d.save()
>>> credit_wallet(d.user, d.amount_usd, source_deposit=d)
```

| Paso | Verificar |
|---|---|
| B-3.1 | Recargar `/deposit/<id>/` → banner "Depósito acreditado" |
| B-3.2 | Sub-texto "$50.00 añadidos a tu wallet." |
| B-3.3 | Botón "Ver Mis Cuentas" visible |
| B-3.4 | En `/home/` — balance de wallet muestra $50.00 |
| B-3.5 | En `/deposit/history/` — depósito aparece con status "finished" |

**Evidencia:** Screenshot de balance + historial. `Wallet.available_balance == 50`.

**PASS:** Balance actualizado, historial correcto.
**FAIL:** Balance sigue en $0 / banner sigue diciendo "Esperando".
**¿Bloqueante?** ✅ SÍ

---

## SECCIÓN C — CHALLENGES

### C-1. Catálogo de challenges

| Paso | Acción | URL |
|---|---|---|
| C-1.1 | Ir al catálogo | `http://127.0.0.1:8000/challenges/` |
| C-1.2 | Verificar que aparece al menos "Internal Test Challenge 10K" | — |
| C-1.3 | Verificar hero, cards con reglas, CTA "Empezar Challenge" | — |
| C-1.4 | Verificar datos dinámicos: precio, account_size, profit_split | — |

**Evidencia:** Screenshot del catálogo con al menos 1 product card.
**PASS:** Cards visibles con datos del ChallengeProduct del Admin.
**FAIL:** Catálogo vacío / error (verificar `is_active=True` en Admin).
**¿Bloqueante?** ✅ SÍ

---

### C-2. Checkout page

| Paso | Acción | URL |
|---|---|---|
| C-2.1 | Click "Empezar Challenge" en la card | `/challenges/<id>/buy/` |
| C-2.2 | Verificar hero: "Checkout Challenge" + "Confirma tu programa de fondeo" | — |
| C-2.3 | Verificar grid izquierdo: tier, nombre, account_size, precio | — |
| C-2.4 | Verificar Phase 1 y Phase 2 rules correctas | — |
| C-2.5 | Verificar profit_split, max_lot_size, max_open_positions | — |
| C-2.6 | Verificar selector de crypto (BTC, ETH, SOL al menos) | — |
| C-2.7 | Verificar botón "Pagar Challenge" y nota de activación automática | — |

**Datos esperados para "Internal Test Challenge 10K":** Validar contra Admin > Challenge Products.

**Evidencia:** Screenshot del checkout con datos completos.
**PASS:** Todos los datos del ChallengeProduct se reflejan.
**FAIL:** Página en blanco / error 404 (producto inactivo).
**¿Bloqueante?** ✅ SÍ

---

### C-3. Pago NOWPayments challenge

| Paso | Acción |
|---|---|
| C-3.1 | Seleccionar BTC, click "Pagar Challenge" |
| C-3.2 | Verificar redirect a `/deposit/<id>/` |
| C-3.3 | Verificar banner "Esperando confirmación del pago" (no "Esperando pago") |
| C-3.4 | Verificar sub-texto "Tu challenge se activará automáticamente…" |
| C-3.5 | Verificar info-row "Tu challenge se activará automáticamente cuando NOWPayments confirme…" |
| C-3.6 | Verificar link "Ver Challenges" (NO "Historial" / "Nuevo depósito") |
| C-3.7 | Verificar `IS_CHALLENGE = true` en source del HTML (Cmd+U → buscar "IS_CHALLENGE") |

**Evidencia:** Screenshot de la página + `Deposit.challenge_product != None` en Admin.

**PASS:** Textos challenge, `IS_CHALLENGE=true`, link "Ver Challenges".
**FAIL:** Muestra textos de wallet, `IS_CHALLENGE=false`.
**¿Bloqueante?** ✅ SÍ

---

### C-4. Simular webhook de confirmación (flujo seguro local)

**Opción A — Management command (recomendado para external_challenge_activate):**
```bash
python manage.py simulate_external_challenge_purchase \
  --email smoketest@moneybroker.local \
  --code TEST_CHALLENGE_10K_2PHASE \
  --url http://127.0.0.1:8000
```

**Opción B — Django shell (para challenge vía NOWPayments):**
```bash
python manage.py shell
>>> from simulator.models import Deposit
>>> from simulator.views import _fulfill_challenge_purchase
>>> import django.db.transaction as txn
>>> d = Deposit.objects.filter(
...     user__email='smoketest@moneybroker.local',
...     challenge_product__isnull=False,
...     credited=False
... ).last()
>>> with txn.atomic():
...     d.credited = True
...     d.status = 'finished'
...     d.save()
...     _fulfill_challenge_purchase(d)
```

| Verificar después | Dónde |
|---|---|
| C-4.1 `ChallengeEnrollment` creado | Admin > Challenge Enrollments |
| C-4.2 `TradingAccount` Phase 1 creada (`account_type="CHALLENGE"`, `phase="Fase 1"`) | Admin > Trading Accounts |
| C-4.3 `RiskRule` creada para esa cuenta | Admin > Risk Rules |
| C-4.4 `ChallengeEnrollment.phase1_account` apunta a la cuenta | Admin > CE > campo phase1_account |
| C-4.5 Email de activación en consola/log: "Tu Challenge X está activo" | Consola servidor |

**Evidencia:** Capturas de Admin de CE + TradingAccount + RiskRule.
**PASS:** Los 3 objetos existen y están enlazados correctamente.
**FAIL:** `_fulfill_challenge_purchase` lanza excepción / objetos no creados.
**¿Bloqueante?** ✅ SÍ (core del flujo challenge)

---

### C-5. Página de status challenge confirmado

| Paso | Verificar | URL |
|---|---|---|
| C-5.1 | Recargar `/deposit/<id>/` | — |
| C-5.2 | Banner "Challenge activado" (no "Depósito acreditado") | — |
| C-5.3 | Sub-texto "Tu cuenta Phase 1 está lista. ¡Comienza a operar!" | — |
| C-5.4 | Botón "Ir a mi cuenta Challenge" con href `/dashboard/<account_id>/` | — |
| C-5.5 | Link "Ver Challenges" presente (NO "Historial" / "Nuevo depósito") | — |
| C-5.6 | `ACCOUNT_URL = "/dashboard/<id>/"` en source HTML | Cmd+U |
| C-5.7 | Click en "Ir a mi cuenta Challenge" → aterriza en dashboard correcto | — |

**Evidencia:** Screenshot del banner + screenshot del dashboard al que lleva.
**PASS:** Botón lleva directamente al dashboard de la cuenta Phase 1.
**FAIL:** Botón dice "Ir al Panel de Trading" y va a `/accounts/`.
**¿Bloqueante?** ✅ SÍ

---

## SECCIÓN D — TRADING

### D-1. Abrir y operar en cuenta challenge

| Paso | Acción | URL |
|---|---|---|
| D-1.1 | Ir al dashboard de la cuenta challenge | `/dashboard/<account_id>/` |
| D-1.2 | Verificar balance inicial = `account_size` del challenge (ej. $10,000) | — |
| D-1.3 | Verificar nombre de cuenta en selector | — |
| D-1.4 | Abrir trade BUY en EUR/USD con 0.01 lots | Panel izquierdo |
| D-1.5 | Verificar línea de entrada (gris-azul dim) en chart | — |
| D-1.6 | Verificar líneas SL (roja dim) y TP (verde dim) | — |

**Evidencia:** Screenshot del dashboard con trade abierto y líneas visibles.
**PASS:** Trade aparece en panel de posiciones, líneas en chart.
**¿Bloqueante?** ✅ SÍ

---

### D-2. Drag de SL/TP

| Paso | Acción |
|---|---|
| D-2.1 | Hacer drag de la línea SL roja hacia abajo |
| D-2.2 | Durante drag: línea SL se pone opaca/brillante (emphasis) |
| D-2.3 | Soltar: precio SL actualizado en panel |

**PASS:** Drag funciona, precio cambia.
**¿Bloqueante?** ⚠️ No bloqueante para staging.

---

### D-3. Cerrar trade y verificar historia persistente

| Paso | Acción |
|---|---|
| D-3.1 | Cerrar trade desde panel |
| D-3.2 | Verificar que aparece en tab "Historial" dentro del dashboard |
| D-3.3 | **Refrescar la página** (F5) |
| D-3.4 | Verificar que el trade sigue en Historial después del refresh |
| D-3.5 | Balance/equity actualizados correctamente |

**Evidencia:** Screenshot del historial antes y después del refresh.
**PASS:** Trade persiste en historial después de F5.
**FAIL:** Historial vacío después de reload.
**¿Bloqueante?** ✅ SÍ (feature K.2D)

---

### D-4. Verificar risk rules activas

| Paso | Verificar |
|---|---|
| D-4.1 | En Admin > Risk Rules: regla asociada a la cuenta tiene valores del ChallengeProduct |
| D-4.2 | `max_daily_loss_pct`, `max_drawdown_pct`, `max_lot_size`, `max_open_positions` correctos |

**Nota:** No simular daily loss / drawdown manualmente en smoke test — puede corromper datos.
**¿Bloqueante?** ⚠️ Solo verificación visual en Admin.

---

## SECCIÓN E — FUNDED / PAYOUTS

### E-1. Verificar elegibilidad visual (requiere KYC aprobado)

| Paso | Acción | URL |
|---|---|---|
| E-1.1 | Ir al dashboard de una cuenta Phase 2/Funded (si existe) | `/dashboard/<id>/` |
| E-1.2 | Verificar que aparece el botón "Solicitar Payout" si es elegible | — |
| E-1.3 | Si no es elegible, verificar que el botón no aparece / está deshabilitado | — |

**Nota:** Para smoke test, la cuenta Phase 1 recién creada no tendrá FundedConfig — verificar solo que no rompe.
**¿Bloqueante?** ⚠️ No bloqueante si no hay cuenta funded.

---

### E-2. Flujo de payout (si hay cuenta funded)

| Paso | Acción | URL |
|---|---|---|
| E-2.1 | Ir a solicitud de payout | `http://127.0.0.1:8000/funded/payout/request/` |
| E-2.2 | Si user tiene 2FA activo: verificar que pide código TOTP antes de procesar | — |
| E-2.3 | Submit payout request | — |
| E-2.4 | Verificar `FundedPayoutRequest` creado en Admin | Admin > Funded Payout Requests |

**¿Bloqueante?** ⚠️ Puede esperar — depende de si hay cuenta funded disponible.

---

## SECCIÓN F — STAFF / ADMIN

### F-1. Login admin

| Paso | Acción | URL |
|---|---|---|
| F-1.1 | Abrir admin (URL según `ADMIN_URL` env — por default `admin/`) | `http://127.0.0.1:8000/admin/` |
| F-1.2 | Login con usuario staff | — |
| F-1.3 | Verificar dashboard de Django Admin carga | — |

**¿Bloqueante?** ✅ SÍ

---

### F-2. ChallengeProduct editable desde Admin

| Paso | Acción | URL |
|---|---|---|
| F-2.1 | Admin > Challenge Products | `http://127.0.0.1:8000/admin/simulator/challengeproduct/` |
| F-2.2 | Abrir el "Internal Test Challenge 10K" | — |
| F-2.3 | Cambiar `p1_profit_target_pct` de 8.00 a 10.00 y guardar | — |
| F-2.4 | Ir a `/challenges/` → card debe mostrar "10%" | — |
| F-2.5 | Ir al checkout `/challenges/<id>/buy/` → Phase 1 section debe mostrar "10%" | — |
| F-2.6 | Revertir el cambio a 8.00 | — |

**Evidencia:** Screenshots de catálogo y checkout con el valor modificado.
**PASS:** Cambio en Admin se refleja inmediatamente en UI sin reiniciar servidor.
**FAIL:** Valor no cambia en frontend.
**¿Bloqueante?** ✅ SÍ

---

### F-3. Staff ops panel (requiere 2FA si `TOTP_STAFF_REQUIRED=True`)

| Paso | Acción | URL |
|---|---|---|
| F-3.1 | Intentar acceder con staff sin 2FA activo | `http://127.0.0.1:8000/staff/ops/` |
| F-3.2 | Verificar redirect a `/account/2fa/verify/` | — |
| F-3.3 | Activar 2FA para staff user | `/account/2fa/setup/` |
| F-3.4 | Volver a `/staff/ops/` → ahora debe cargar | — |
| F-3.5 | Verificar secciones: usuarios, depósitos, cuentas, métricas | — |

**Evidencia:** Screenshot del ops panel.
**PASS:** Ops panel carga solo con 2FA verificado.
**FAIL:** Ops panel accesible sin 2FA / error 500.
**¿Bloqueante?** ✅ SÍ (seguridad crítica)

---

### F-4. Endpoints staff bloqueados sin 2FA

| Paso | Verificar |
|---|---|
| F-4.1 | Desde sesión staff sin 2FA verificado, hacer GET a `/api/broker/monitoring/` |
| F-4.2 | Debe responder `{"error": "2fa_required"}` con status 403 |
| F-4.3 | Mismo check para `/api/broker/snapshots/` y `/api/metrics/` |

**Evidencia:** Respuesta JSON en browser DevTools o curl.
**PASS:** 403 `2fa_required`.
**FAIL:** 200 con datos sin autenticar 2FA.
**¿Bloqueante?** ✅ SÍ (seguridad)

---

## SECCIÓN G — EMAILS

> **Setup local:** Con `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` todos los emails salen por consola del servidor.

| ID | Email | Cuándo se envía | Cómo verificar |
|---|---|---|---|
| G-1 | Verificación de email | Al registrarse | Consola: buscar `verify-email/` |
| G-2 | Depósito confirmado | Webhook NOWPayments / shell manual `send_deposit_confirmed_email(d)` | Consola: "Depósito confirmado" |
| G-3 | Challenge activo | `_fulfill_challenge_purchase` ejecutado | Consola: "Tu Challenge X está activo" |
| G-4 | Soporte ticket | POST a `/support/` | Consola: email de confirmación al usuario + email admin |
| G-5 | KYC aprobado/rechazado | Admin cambia KYC status | Consola: `send_kyc_approved_email` |

**Para G-2 manual sin webhook:**
```bash
python manage.py shell
>>> from simulator.deposit_emails import send_deposit_confirmed_email
>>> from simulator.models import Deposit
>>> send_deposit_confirmed_email(Deposit.objects.last())
```

**¿Bloqueante?** ⚠️ G-1 y G-3 son bloqueantes. G-4, G-5 pueden esperar.

---

## SECCIÓN H — SEGURIDAD BÁSICA

### H-1. Usuario no autenticado no accede a rutas privadas

| URL a probar (sin login) | Resultado esperado |
|---|---|
| `http://127.0.0.1:8000/deposit/` | Redirect a `/login/?next=/deposit/` |
| `http://127.0.0.1:8000/dashboard/` | Redirect a `/login/` |
| `http://127.0.0.1:8000/challenges/` | Redirect a `/login/` |
| `http://127.0.0.1:8000/staff/ops/` | Redirect a `/login/` |
| `http://127.0.0.1:8000/funded/payout/request/` | Redirect a `/login/` |

**PASS:** Todas redirigen. Ninguna da 200 sin sesión.
**¿Bloqueante?** ✅ SÍ

---

### H-2. Producto inactivo no comprable

| Paso | Acción |
|---|---|
| H-2.1 | En Admin: marcar el challenge product como `is_active=False` |
| H-2.2 | Intentar acceder a `/challenges/<id>/buy/` con id de ese producto |
| H-2.3 | Resultado esperado: redirect a `/challenges/` |
| H-2.4 | Reactivar el producto |

**PASS:** Redirect a catálogo.
**FAIL:** Checkout carga con producto inactivo.
**¿Bloqueante?** ✅ SÍ

---

### H-3. Status de depósito ajeno no visible

| Paso | Acción |
|---|---|
| H-3.1 | Anotar el `deposit_id` del usuario `smoketest_01` |
| H-3.2 | Login como otro usuario diferente |
| H-3.3 | Acceder a `http://127.0.0.1:8000/deposit/<id>/` con el id del otro usuario |
| H-3.4 | Resultado esperado: redirect a `/deposit/history/` (no 200 con datos ajenos) |

**PASS:** Redirect sin datos del otro usuario.
**FAIL:** Muestra datos del depósito ajeno.
**¿Bloqueante?** ✅ SÍ

---

### H-4. Endpoint JSON de status ajeno bloqueado

```bash
curl -b "sessionid=<cookie_usuario_B>" \
     http://127.0.0.1:8000/deposit/<id_usuario_A>/status.json
```

**Resultado esperado:** `{"error": "not found"}` con status 404.

**¿Bloqueante?** ✅ SÍ

---

## RESUMEN DE PRIORIDADES

### Bloqueantes para staging (deben pasar antes de deploy a VPS)

| ID | Descripción |
|---|---|
| A-1, A-2, A-3, A-4 | Auth completo (registro → email → login → terms) |
| B-1, B-3 | Depósito wallet funciona y acredita balance |
| C-1, C-2, C-3 | Catálogo → checkout → pago challenge fluye |
| C-4, C-5 | Webhook activa enrollment + botón directo a cuenta |
| D-1, D-3 | Trading abre/cierra, historial persiste después de refresh |
| F-1, F-2 | Admin funciona, ChallengeProduct editable |
| F-3, F-4 | Ops panel protegido por 2FA |
| G-1, G-3 | Emails de verificación y activación challenge |
| H-1, H-2, H-3, H-4 | Todas las barreras de seguridad básica |

### Pueden esperar (staging no bloqueante)

| ID | Descripción |
|---|---|
| A-5 | 2FA usuario normal (configuración personal) |
| A-6 | KYC (solo necesario para retiros) |
| D-2 | Drag SL/TP (UX, no funcional crítico) |
| D-4 | Risk rules admin check (visual) |
| E-1, E-2 | Funded payout (requiere cuenta funded completa) |
| G-4, G-5 | Emails support y KYC |

---

## PLANTILLA DE REGISTRO PASS/FAIL

```
SMOKE TEST K.3 — <fecha> — entorno: <local/staging>
Usuario: smoketest_01 @ <url>
Ejecutado por: <nombre>

[ ] A-1  Registro                           PASS / FAIL  Nota: ___
[ ] A-2  Email verification                 PASS / FAIL  Nota: ___
[ ] A-3  Login                              PASS / FAIL  Nota: ___
[ ] A-4  Terms acceptance                   PASS / FAIL  Nota: ___
[ ] B-1  Crear depósito wallet              PASS / FAIL  Nota: ___
[ ] B-3  Confirmar depósito + balance       PASS / FAIL  Nota: ___
[ ] C-1  Catálogo challenges               PASS / FAIL  Nota: ___
[ ] C-2  Checkout page                      PASS / FAIL  Nota: ___
[ ] C-3  Pago challenge + status pending    PASS / FAIL  Nota: ___
[ ] C-4  Webhook → enrollment + account     PASS / FAIL  Nota: ___
[ ] C-5  Status credited + botón directo    PASS / FAIL  Nota: ___
[ ] D-1  Trading abre trade                 PASS / FAIL  Nota: ___
[ ] D-3  Historial persiste post-refresh    PASS / FAIL  Nota: ___
[ ] F-1  Admin login                        PASS / FAIL  Nota: ___
[ ] F-2  ChallengeProduct editable          PASS / FAIL  Nota: ___
[ ] F-3  Ops panel protegido 2FA            PASS / FAIL  Nota: ___
[ ] F-4  API staff bloqueada sin 2FA        PASS / FAIL  Nota: ___
[ ] G-1  Email verificación en log          PASS / FAIL  Nota: ___
[ ] G-3  Email challenge activo en log      PASS / FAIL  Nota: ___
[ ] H-1  Rutas privadas sin login           PASS / FAIL  Nota: ___
[ ] H-2  Producto inactivo redirige         PASS / FAIL  Nota: ___
[ ] H-3  Depósito ajeno bloqueado           PASS / FAIL  Nota: ___
[ ] H-4  JSON ajeno retorna 404             PASS / FAIL  Nota: ___

RESULTADO FINAL: ___ / 23 PASS
¿LISTO PARA STAGING?: SÍ / NO
```
