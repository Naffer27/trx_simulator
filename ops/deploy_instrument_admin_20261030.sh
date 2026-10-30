#!/bin/bash
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'Requiere sudo para respaldar e instalar.'; exit 1; }
export APP=/opt/trx_sim PACKAGE=/home/naffer/money-instrument-admin-release PYTHONDONTWRITEBYTECODE=1
PY=$APP/venv-security-a9243aa/bin/python
cd "$APP"
python3 - <<'PY'
import ast,hashlib,json,os
from pathlib import Path
app=Path(os.environ['APP']);pkg=Path(os.environ['PACKAGE'])
for rel,m in json.loads((pkg/'manifest.json').read_text()).items():
 assert rel in ('simulator/admin.py','simulator/instrument_runtime_preview.py')
 assert hashlib.sha256((pkg/rel).read_bytes()).hexdigest()==m['after'], 'Paquete alterado: '+rel
 ast.parse((pkg/rel).read_text())
 if m['before'] is None: assert not (app/rel).exists(), 'Archivo nuevo ya existe: '+rel
 else: assert hashlib.sha256((app/rel).read_bytes()).hexdigest()==m['before'], 'Version remota distinta: '+rel
print('Version y paquete verificados.')
PY
"$PY" -B manage.py check
check_http() {
"$PY" -B manage.py shell <<'PYHTTP'
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from django.urls import reverse

for label, route in (("Portada", "/"), ("Acceso al administrador configurado", reverse("admin:login"))):
    for attempt in range(10):
        try:
            with urllib.request.urlopen("https://moneybrokersglobal.com" + route, timeout=15) as response:
                if response.status != 200 or urlsplit(response.geturl()).path != route:
                    raise RuntimeError(label + ": respuesta o redireccion inesperada")
            print(label + ": HTTP 200")
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (502, 503, 504) or attempt == 9:
                raise RuntimeError(label + ": HTTP " + str(exc.code)) from None
            time.sleep(2)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 9:
                raise RuntimeError(label + ": no se pudo conectar") from None
            time.sleep(2)
PYHTTP
}
# Verify the actual configured URL before any installation.
check_http
export BACKUP=/root/money-instrument-admin-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$BACKUP"
python3 - <<'PY'
import os,json,shutil
from pathlib import Path
app=Path(os.environ['APP']);pkg=Path(os.environ['PACKAGE']);b=Path(os.environ['BACKUP'])
for rel,m in json.loads((pkg/'manifest.json').read_text()).items():
 if m['before']:
  t=b/rel;t.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(app/rel,t)
shutil.copy2(pkg/'manifest.json',b/'manifest.json')
PY
rollback() {
 trap - ERR
 echo 'Fallo: restaurando archivos anteriores.'
 python3 - <<'PY'
import os,json,shutil
from pathlib import Path
app=Path(os.environ['APP']);b=Path(os.environ['BACKUP'])
for rel,m in json.loads((b/'manifest.json').read_text()).items():
 if m['before']:
  tmp=(app/rel).with_suffix('.restore-tmp');shutil.copy2(b/rel,tmp);tmp.replace(app/rel)
 else: (app/rel).unlink(missing_ok=True)
PY
 systemctl restart daphne || true
 echo "Revisar servicios. Respaldo: $BACKUP"
 exit 1
}
trap rollback ERR
python3 - <<'PY'
import os,json,shutil
from pathlib import Path
app=Path(os.environ['APP']);pkg=Path(os.environ['PACKAGE'])
for rel in json.loads((pkg/'manifest.json').read_text()):
 target=app/rel;tmp=target.with_suffix('.release-tmp');tmp.write_bytes((pkg/rel).read_bytes())
 if target.exists():
  st=target.stat();os.chown(tmp,st.st_uid,st.st_gid);tmp.chmod(st.st_mode & 0o777)
 else: shutil.chown(tmp,user='naffer',group='trx_sim');tmp.chmod(0o644)
 tmp.replace(target)
PY
"$PY" -B manage.py check
"$PY" -B manage.py shell -c 'from simulator.admin import InstrumentAdmin; from simulator.models import Instrument; from django.contrib.admin.sites import AdminSite; from market_data.symbol_specs import allowed_symbols; from django.conf import settings; p=InstrumentAdmin(Instrument,AdminSite()); assert "runtime_specification" in p.readonly_fields; assert "Sin especificación" in p.runtime_specification(Instrument(symbol="KASUSD")); assert allowed_symbols()==frozenset(["EUR/USD","GBP/USD","USD/JPY","AUD/USD","BTCUSD","ETHUSD"]); assert not settings.PENDING_ORDER_SPREAD_FEE_ENABLED; print("Ficha importada. Instrumentos y cobro pendientes siguen desactivados.")'
systemctl restart daphne
check_http

systemctl is-active daphne celery-worker celery-beat
printf 'PUBLICADA FICHA DE CONSULTA. Respaldo: %s. Sin migraciones ni activación de instrumentos.\n' "$BACKUP"
