# Registro público e inicio de sesión con TOTP

## Comportamiento

- El registro ya no pide código beta, incluso si el servidor conserva BROKER_ACCESS_CODE. La atribución de referidos y la verificación de correo existentes no cambian.
- Las cuentas sin dispositivo TOTP confirmado conservan su acceso con contraseña. La activación se hace en la pantalla 2FA existente, compatible con Authy.
- Las cuentas con TOTP confirmado reciben un desafío después de la contraseña. No se crea una sesión autenticada hasta que el código sea válido, por lo que el desafío pendiente tampoco concede acceso a APIs o WebSockets autenticados.
- El desafío firmado vence en cinco minutos y queda invalidado al cambiar contraseña, desactivar la cuenta o sustituir/eliminar el dispositivo.
- Los intentos se cuentan por dispositivo en la base de datos, no por navegador: cinco fallos bloquean durante cinco minutos. Reiniciar el paso de contraseña no reinicia el contador. Un paso TOTP aceptado para iniciar sesión no puede reutilizarse para otro inicio de sesión.
- El verificador de retiros y las comprobaciones del dashboard no cambian. Tampoco se modifican motores, consumers, webhooks ni tareas financieras.

## Alcance y límites

No introduce inscripción 2FA obligatoria para todos, códigos de recuperación, una política nueva de recuperación de cuentas ni revocación masiva de sesiones ya abiertas. No modifica el acceso administrativo de Django. El bloqueo por dispositivo se serializa mediante select_for_update en PostgreSQL; SQLite no ofrece ese bloqueo por fila.

## Despliegue

1. Respaldar la base del destino y comprobar el estado del repositorio.
2. Desplegar los archivos de esta modificación y aplicar la migración 0100_totp_login_security antes de arrancar los procesos nuevos. Revisar primero el plan: no ejecutar migraciones ajenas sin revisarlas.
3. Reiniciar los procesos que sirven la aplicación y comprobar el registro sin código, acceso normal sin 2FA y acceso con 2FA usando una cuenta de prueba propia.
4. Verificar los flujos existentes de correo/referidos y los controles de retiro sin efectuar transferencias de prueba reales.

La migración añade únicamente tres campos a TOTPDevice. No cambia datos de cuentas, saldos u operaciones. BROKER_ACCESS_CODE queda como configuración heredada ignorada; no hace falta revelar ni compartir su valor.

## Validación local

82 pruebas de registro, login, TOTP nuevo, 2FA de personal/retiros y atribución de referidos. Base SQLite de pruebas en memoria, correo simulado y conexiones de red bloqueadas. No certifica por sí sola el despliegue PostgreSQL ni la entrega de correo del VPS.
