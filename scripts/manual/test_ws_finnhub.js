// scripts/manual/test_ws_finnhub.js
//
// Manual diagnostic: connect to the Finnhub WS and subscribe to a couple of
// symbols. Not part of any automated test suite or runtime path — run by
// hand only, from the repo root:
//
//   FINNHUB_API_KEY=xxxx node scripts/manual/test_ws_finnhub.js
//
// Requires FINNHUB_API_KEY in the environment. Never hardcode the key here.

const WebSocket = require('ws');

const FINNHUB_API_KEY = process.env.FINNHUB_API_KEY;

if (!FINNHUB_API_KEY) {
  console.error('FINNHUB_API_KEY no está configurada en el entorno.');
  console.error('Uso: FINNHUB_API_KEY=xxxx node scripts/manual/test_ws_finnhub.js');
  process.exit(1);
}

const socket = new WebSocket(`wss://ws.finnhub.io?token=${FINNHUB_API_KEY}`);

socket.addEventListener('open', function () {
  console.log('✅ Conectado');
  socket.send(JSON.stringify({ type: 'subscribe', symbol: 'BINANCE:BTCUSDT' }));
  socket.send(JSON.stringify({ type: 'subscribe', symbol: 'AAPL' }));
});

socket.addEventListener('message', function (event) {
  console.log('📨 Mensaje:', event.data);
});

socket.addEventListener('error', function (event) {
  console.error('❌ Error:', event.message || event);
});
