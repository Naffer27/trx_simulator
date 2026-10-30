from decimal import Decimal
from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase
from simulator.admin import InstrumentAdmin
from simulator.models import Instrument
from simulator.instrument_runtime_preview import runtime_comparison
from market_data.symbol_specs import allowed_symbols, get_spec, normalize_symbol


def reference(symbol):
    spec = get_spec(normalize_symbol(symbol))
    names = ('contract_size', 'min_lot', 'max_lot', 'lot_step', 'tick_size',
             'pip_size', 'price_decimals', 'max_leverage', 'margin_mode', 'quote_currency')
    return Instrument(symbol=symbol, trading_enabled=spec.enabled,
                      **{name: getattr(spec, name) for name in names})


class RuntimePreviewTests(SimpleTestCase):
    # SimpleTestCase forbids database queries: viewing these rules cannot alter
    # account balances, orders or the reference catalogue.
    def test_known_contract_sizes_and_lot_steps_are_read_from_runtime(self):
        for symbol, contract, step in [('BTCUSD', '1', '.001'), ('XAUUSD', '100', '.01'),
                                       ('US30', '1', '.1'), ('SOLUSD', '1', '.1')]:
            with self.subTest(symbol=symbol):
                preview = runtime_comparison(reference(symbol))
                rows = {label: (live, same) for label, live, _, same in preview['rows']}
                self.assertEqual(Decimal(rows['Unidades por lote'][0]), Decimal(contract))
                self.assertEqual(Decimal(rows['Paso de lotaje'][0]), Decimal(step))
                self.assertTrue(all(row[3] for row in preview['rows']))

    def test_editing_catalogue_does_not_change_runtime(self):
        obj = reference('BTCUSD')
        original = get_spec('BTCUSD')
        before = allowed_symbols()
        obj.contract_size = Decimal('999')
        obj.max_leverage = 500
        obj.trading_enabled = False
        preview = runtime_comparison(obj)
        differences = {row[0] for row in preview['rows'] if not row[3]}
        self.assertEqual(differences, {'Unidades por lote',
                                     'Límite de apalancamiento del instrumento',
                                     'Habilitado en registro'})
        self.assertEqual(get_spec('BTCUSD'), original)
        self.assertEqual(allowed_symbols(), before)

    def test_unregistered_targets_do_not_get_fabricated_rules(self):
        for symbol in ('USOIL', 'WTI', 'KASUSD'):
            self.assertIsNone(runtime_comparison(Instrument(symbol=symbol)))

    def test_disabled_targets_remain_disabled_even_if_reference_enabled(self):
        for symbol in ('XAUUSD', 'US30', 'SOLUSD'):
            obj = reference(symbol)
            obj.trading_enabled = True
            preview = runtime_comparison(obj)
            self.assertFalse(preview['enabled'])
            self.assertNotIn(preview['symbol'], allowed_symbols())

    def test_admin_renders_actual_rules_and_preserves_warning(self):
        panel = InstrumentAdmin(Instrument, AdminSite())
        rendered = str(panel.runtime_specification(reference('XAUUSD')))
        self.assertIn('XAU/USD', rendered)
        self.assertIn('Desactivado', rendered)
        self.assertIn('no es una cotización', rendered)
        self.assertIn('runtime_specification', panel.readonly_fields)
        self.assertIn('NINGÚN campo editado aquí afecta', panel._RUNTIME_WARNING)

    def test_admin_escapes_reference_values(self):
        obj = reference('BTCUSD')
        obj.quote_currency = '<script>alert(1)</script>'
        html = str(InstrumentAdmin(Instrument, AdminSite()).runtime_specification(obj))
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_unknown_symbol_admin_message(self):
        panel = InstrumentAdmin(Instrument, AdminSite())
        self.assertIn('Sin especificación', panel.runtime_specification(Instrument(symbol='KASUSD')))
