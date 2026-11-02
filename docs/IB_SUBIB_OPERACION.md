# IB y Sub-IB: operación de la red

Estado: implementación probada localmente; activar únicamente con el paquete validado del VPS.

## Reparto inicial

- IB directo: regla PER_LOT existente, actualmente $5/lote.
- Primer superior: SUB_IB_PER_LOT de $0,50/lote; cada nivel siguiente usa la mitad de la tarifa base aplicable al beneficiario.
- Presupuesto para todos los superiores: SUB_IB_BUDGET de $1/lote, aplicable al IB directo. Se distribuye del superior inmediato hacia arriba hasta agotarlo.
- Con valores iniciales, el gasto nominal es como máximo $6/lote. Subir la tarifa directa o el presupuesto aumenta este máximo; no implica que el margen del broker esté garantizado.
- Las reglas individuales prevalecen sobre las globales. Elevar una tarifa de superior no permite exceder el presupuesto de la red.
- No se pagan altas ni reclutamiento. DEMO y cuentas de desafíos quedan excluidas del cálculo por lote.

## Configurar y vincular

En administración, Directorio IB → detalle del IB → cambiar tarifa permite elegir clientes directos, primer nivel Sub-IB o presupuesto de superiores. Se crea una regla nueva con vigencia actual; las obligaciones históricas conservan su importe.

La administración de IB Commission Rules también permite crear reglas globales o individuales con vigencia explícita. Evitar períodos superpuestos: una ambigüedad bloquea el cálculo y queda registrada para revisión.

En Red de IB y Sub-IB se puede asignar un superior a un IB existente indicando motivo. Un IB tiene un solo superior; puede tener cualquier cantidad de hijos. No se impone un máximo comercial de niveles. Se rechazan ciclos y autorreferencias. Los vínculos históricos no se editan ni se borran desde administración.

Si una persona registrada mediante un enlace de referido crea después su perfil IB, se conserva automáticamente el introductor original como superior. Las relaciones de usuarios existentes anteriores a este despliegue se revisan y vinculan desde administración; no se inventan relaciones retrospectivas.

## Cálculo y abono

La tarea periódica de comisiones procesa operaciones posteriores a la activación. Guarda una marca por ejecución; lotes posteriores al límite de cada pasada permanecen pendientes para la siguiente. Reintentar no duplica el cálculo. Los errores se conservan para reintento y se registran en logs.

Las fracciones menores de centavo se acumulan exactamente por beneficiario usando racionales. Al alcanzar un centavo se genera una obligación pendiente; sus metadatos identifican el incremento, el resto anterior y el resto nuevo. Un abono puede incluir fracciones de operaciones anteriores: no debe interpretarse como una comisión exclusiva del último cliente. Los registros IBNetworkAccrual conservan cada aportación y su origen.

Cada obligación sigue el circuito existente: aprobación IB → solicitud de tesorería → aprobación y ejecución de tesorería → conciliación CREDITED. La activación del programa no ejecuta pagos ni aprueba solicitudes automáticamente. CREDITED significa abono a la cartera interna; el retiro externo es un proceso distinto.

Los IB congelados no acumulan nuevas comisiones durante ese estado. Su nivel no se elimina de la cadena ni incrementa el importe de niveles superiores. Los abonos pendientes conservan los bloqueos y controles de tesorería. Los reversos se gestionan con ajustes auditados, sin borrar obligaciones.

## Comprobación después de activar

1. Verificar las reglas $5 / $0,50 / $1 y los vínculos esperados en administración.
2. Verificar que la tarea de comisiones y la conciliación siguen programadas.
3. Revisar Red IB, acumulaciones y obligaciones tras una operación elegible autorizada; no usar operaciones DEMO como evidencia de un pago real.
4. Aprobar y abonar únicamente las obligaciones revisadas, comprobando cartera y estado CREDITED.

Pruebas locales: permisos de administración, límites y duplicados, genealogía de 30 niveles, fracciones de centavo, presupuesto, tarifas individuales, protección de DEMO, recuperación por lotes, aprobación y abono a ambos beneficiarios. No constituyen una prueba de pago en producción.
