const WebSocket = require('ws');

const socket = new WebSocket('wss://ws.finnhub.io?token=d26gc99r01qvraiq9gtgd26gc99r01qvraiq9gu0');

socket.addEventListener('open', function () {
  console.log('✅ Conectado');
  socket.send(JSON.stringify({ type: 'subscribe', symbol: 'BINANCE:BTCUSDT' }));
  socket.send(JSON.stringify({ type: 'subscribe', symbol: 'AAPL' }));
});

socket.addEventListener('message', function (event) {
  console.log('📨 Mensaje:', event.data);
});
