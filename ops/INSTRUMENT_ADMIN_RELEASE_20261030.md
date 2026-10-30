# Instrument admin release

Deploy only simulator/admin.py and simulator/instrument_runtime_preview.py.
This exposes a read-only runtime/reference comparison in the existing Instrument
admin detail. Pilot economic rules are saved in Git but NOT deployed or enabled.

84 tests passed in /tmp/money-instrument-preview.C2WGJT/tests.log; all six source
and test files were compared byte-for-byte with that validated archive before
commit. No re-run required: the remaining changes were documentation/evidence.

Package: /tmp/money-instrument-admin-release
Destination: /home/naffer/money-instrument-admin-release
Script requires root to back up files under /root, install atomically and restart
daphne. Every target's pre-image hash is checked; new helper must be absent.
Restoration runs on installation/check/health failure. Does not restart Celery,
run migrations, write database records or change environment flags.

VPS inspection: accounts 3/4 belong to ADMIN2, 5/6 to blameus30fx; all active
DEMO accounts with ECN 7 / VIP 3.50 USD per lot at opening. BTC contract=1,
minimum/step=.001, leverage cap=20. ADMIN2 demo #3 dashboard loaded and showed
changing BTC bid/ask on 2026-10-30. No order was submitted by this agent.

Publication still requires the operator's sudo password. Upload alone is not
deployment. After running deploy.sh, open /admin/simulator/instrument/ and select
an existing symbol to see the comparison. No new instruments become tradeable.

## Health-check correction after first deployment attempt

The initial release installed/imported successfully but its hardcoded
/admin/login/ request returned 404; the error trap restored the previous files.
Confirmed remotely: original admin.py SHA256 restored, new helper absent, all
three services active. Actual reverse('admin:login') returns a custom route
and its public URL responds HTTP 200.

The script now resolves admin:login inside the deployed Django environment,
checks that URL before any installation and again after restart, and rejects
unexpected redirects. Only transient 502/503/504 or connection errors retry;
404 fails immediately. No custom admin URL is printed. Source application files
and the 84-test validation are unchanged by this script-only correction.
