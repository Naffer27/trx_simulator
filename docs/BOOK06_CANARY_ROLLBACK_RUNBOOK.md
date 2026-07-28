# BOOK-06 — Canary Rollback Runbook

**Alcance:** activación, operación y rollback del canario de exposición ajustada del Dealing Desk (`DEALING_DESK_EXPOSURE_ENABLED` / `DEALING_DESK_EXPOSURE_ACCOUNT_IDS`), tal como quedó implementado en BOOK-06g → BOOK-06h.3 y validado en BOOK-06h.4.

**No cubre:** activación real en un entorno de producción fuera de este proyecto, ni la decisión de negocio/riesgo sobre el Hallazgo F-01 del RC-1 (ver `docs/BOOK06_RC1_AUDIT.md`, Sección 16) — ese reconocimiento debe obtenerse ANTES de seguir este runbook fuera de un ensayo controlado.

---

## 1. Activar el canario

1. Confirmar que el commit desplegado incluye al menos hasta `book-06h.3` (`3dd271b91b3fa8c8e35434f182a4510fab2c2968`) — sin esto, el resolver no está siquiera cableado en `validate_new_order()`.
2. Elegir **exactamente una** cuenta interna de prueba (nunca un cliente real). Anotar su `account_id`.
3. Configurar las variables de entorno del proceso:
   ```
   DEALING_DESK_EXPOSURE_ENABLED=True
   DEALING_DESK_EXPOSURE_ACCOUNT_IDS=<account_id>
   ```
   Para más de una cuenta, separar por comas: `DEALING_DESK_EXPOSURE_ACCOUNT_IDS=101,102`.
4. **Reiniciar el proceso** (ver Sección 6 — obligatorio, no hay recarga en caliente).

---

## 2. Verificar estado

1. Confirmar en shell/Django que la configuración cargó:
   ```python
   from django.conf import settings
   settings.DEALING_DESK_EXPOSURE_ENABLED       # debe ser True
   settings.DEALING_DESK_EXPOSURE_ACCOUNT_IDS   # debe ser frozenset({<account_id>, ...})
   ```
2. Confirmar que la cuenta elegida tiene al menos una `DealingDeskDecision(is_simulated_hedge=True)` real (generada por tráfico propio, nunca fabricada):
   ```python
   from simulator.models import DealingDeskDecision
   DealingDeskDecision.objects.filter(
       is_simulated_hedge=True,
       routing_decision__account_id=<account_id>,
   ).exists()   # debe ser True antes de considerar el trial "activo"
   ```

---

## 3. Verificar logs

Con el canario activo y al menos una orden validada para la cuenta elegida, buscar en los logs de `simulator.broker_risk`:

```
[broker_risk] dealing_desk_exposure_used account_id=<id> mode=adjusted
  excluded_positions_count=<N> excluded_notional=<monto>
  official_gross_notional=<monto> adjusted_gross_notional=<monto>
  official_net_notional=<monto> adjusted_net_notional=<monto>
  risk_allowed=<True/False> reason_code=<código o None>
```

- **Presencia** de esta línea confirma que el libro ajustado se usó realmente para esa orden (cierra RC-1 Finding F-05).
- **Ausencia** total de esta línea durante el trial, pese a tener el flag ON y la cuenta en el allowlist, es una señal de alerta — revisar si la cuenta realmente tiene decisiones `is_simulated_hedge=True` o si el log de fallo (abajo) está apareciendo en su lugar.
- Cualquier aparición de:
  ```
  [broker_risk] dealing desk exposure resolution failed for account=<id>: ...
  ```
  indica que el resolver cayó al cálculo oficial (fail-safe) — la orden se validó igual, sin interrupción, pero debe investigarse la causa antes de continuar el trial.

---

## 4. Verificar admin (BOOK-06e)

1. Ingresar como superusuario a `/admin/simulator/brokersnapshot/shadow-exposure/` (`admin:broker_shadow_exposure`).
2. Confirmar el indicador **Canary Status** en la parte superior:
   - Debe mostrar **ON** con el flag activo, y el conteo correcto de cuentas configuradas en `DEALING_DESK_EXPOSURE_ACCOUNT_IDS`.
3. **Importante:** el resto de la pantalla (Gross Exposure Shadow, etc.) es un cálculo **global** e independiente del allowlist (ver `broker_risk_shadow.py`) — no representa el alcance real del canario activo. El único indicador que refleja la configuración real es la barra "Canary Status".

---

## 5. Rollback

1. Cambiar la variable de entorno:
   ```
   DEALING_DESK_EXPOSURE_ENABLED=False
   ```
   (o, alternativamente, vaciar `DEALING_DESK_EXPOSURE_ACCOUNT_IDS=` — cualquiera de las dos neutraliza el canario).
2. **Reiniciar el proceso** (obligatorio — ver Sección 6).
3. El rollback **no requiere ninguna limpieza de datos**: las filas de `DealingDeskDecision` ya escritas permanecen exactamente como están, no se borran ni se modifican. Desactivar el flag solo detiene su uso futuro en la resolución de exposición.

---

## 6. Reinicio — si aplica

**Siempre aplica.** `DEALING_DESK_EXPOSURE_ENABLED`/`DEALING_DESK_EXPOSURE_ACCOUNT_IDS` se leen vía `os.getenv()` una única vez, en el momento en que `trx_simulator/settings.py` se importa (arranque del proceso Daphne/gunicorn/runserver) — no existe recarga en caliente ni caché intermedia que invalidar. Este es el mismo comportamiento que ya rige `LIQUIDITY_ENGINE_ENABLED`/`ROUTING_ENGINE_ENABLED`, no una particularidad del canario.

Tanto la **activación** como el **rollback** requieren reiniciar el proceso para surtir efecto.

---

## 7. Validaciones posteriores

Después de activar (o de hacer rollback), confirmar:

1. `python manage.py check` sin errores.
2. La cuenta canario abre/valida órdenes con normalidad (sin excepciones visibles al trader — el fail-safe garantiza esto, pero debe confirmarse en la práctica).
3. Ninguna otra cuenta (fuera del allowlist) muestra ningún cambio de comportamiento.
4. `balance`/`equity`/`margin`/P&L de la cuenta canario y de cualquier otra cuenta permanecen consistentes con lo esperado (BOOK-06 nunca los toca, ver RC-1 Sección 13).
5. Tras un rollback, una nueva validación para la misma cuenta produce exactamente el mismo resultado que antes de haber activado el canario (ver `test_off_on_off_restores_identical_decision`, BOOK-06h.4).

---

## 8. Criterios de éxito

- El trial completo transcurre sin disparar ningún criterio de abortar (Sección 9).
- Se observó al menos una vez el log `dealing_desk_exposure_used` para la cuenta canario, confirmando que el mecanismo se usó realmente, no solo que estaba configurado.
- El comportamiento no-monotónico de `MAX_NET_NOTIONAL` (RC-1, Hallazgo F-01) fue observado y entendido por el equipo, o deliberadamente no se dio durante el período — en cualquier caso, sin sorpresas para el equipo operador.
- El rollback (Sección 5) restaura el comportamiento oficial exacto, verificado por al menos una validación posterior (Sección 7, punto 5).

## 9. Criterios de abortar

- Cualquier excepción real del resolver que se repita de forma sostenida (más allá de un evento aislado ya cubierto por el fail-safe).
- Cualquier discrepancia entre el comportamiento observado y lo documentado en el RC-1 o en este runbook.
- Cualquier cambio inesperado en balance/equity/margin/P&L/ledger de **cualquier** cuenta, no solo la canario.
- Ausencia sostenida del log `dealing_desk_exposure_used` cuando se esperaba verlo (indicio de que el canario no se está usando realmente, pese a estar configurado).
- Cualquier señal de que el indicador de BOOK-06e no refleja la configuración real (inconsistencia entre `settings` y lo mostrado en pantalla).

## 10. Checklist operacional

```
[ ] Commit desplegado incluye book-06h.3 o posterior
[ ] Cuenta canario elegida (una sola, interna, nunca cliente real)
[ ] DEALING_DESK_EXPOSURE_ENABLED=True configurado
[ ] DEALING_DESK_EXPOSURE_ACCOUNT_IDS=<account_id> configurado
[ ] Proceso reiniciado
[ ] settings.DEALING_DESK_EXPOSURE_ENABLED / ACCOUNT_IDS verificados en shell
[ ] Cuenta tiene al menos una DealingDeskDecision(is_simulated_hedge=True) real
[ ] Indicador "Canary Status" en BOOK-06e muestra ON con el conteo correcto
[ ] Al menos un log dealing_desk_exposure_used observado para la cuenta
[ ] Ningún log de fallback/error sostenido
[ ] Período de observación completado (2-4 semanas recomendadas, RC-1 Sección 14)
[ ] Reconocimiento de negocio/riesgo sobre F-01 obtenido explícitamente
[ ] Decisión: aprobar expansión, o iniciar rollback
[ ] (si rollback) DEALING_DESK_EXPOSURE_ENABLED=False configurado
[ ] (si rollback) Proceso reiniciado
[ ] (si rollback) Validación posterior confirma comportamiento oficial idéntico al previo a la activación
```
