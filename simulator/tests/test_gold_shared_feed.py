"""Exercise the real shared reader with a scripted provider, never the network."""
import json
from unittest.mock import patch, AsyncMock
from simulator.tests.test_fix05b3_massive_forex_live import (
    _MassiveForexLiveTestsBase, _make_connect_mock, _run, _settle,
    MASSIVE_AUTH_SUCCESS,
)
from market_data.feeds import _MASSIVE_WS_ENABLED_SYMBOLS


class GoldSharedFeedTests(_MassiveForexLiveTestsBase):
    def test_gold_reuses_forex_connection_and_routes_its_own_quote(self):
        quote=json.dumps([{'ev':'C','p':'XAU/USD','b':4000,'a':4000.3,'t':1793393972000}])
        connection=_make_connect_mock([[MASSIVE_AUTH_SUCCESS,quote]])
        async def scenario():
            with patch('market_data.feeds._MASSIVE_WS_ENABLED_SYMBOLS',
                       _MASSIVE_WS_ENABLED_SYMBOLS | {'XAU/USD'}), \
                 patch('market_data.feeds.websockets.connect',connection), \
                 patch.object(self.fm,'_broadcast',new=AsyncMock()) as broadcast:
                await self.fm._massive_register_symbol('EUR/USD',self.channel_layer)
                await self.fm._massive_register_symbol('XAU/USD',self.channel_layer)
                await _settle()
                self.assertEqual(connection.call_count,1)
                messages=[json.loads(s) for s in connection.instances[0].sent]
                self.assertEqual(len([m for m in messages if m['action']=='auth']),1)
                self.assertTrue(any('C.XAU/USD' in m.get('params','') for m in messages if m['action']=='subscribe'))
                broadcast.assert_awaited_once_with('XAU/USD',self.channel_layer,4000,4000.3,1793393972,source='massive')
        _run(scenario())
