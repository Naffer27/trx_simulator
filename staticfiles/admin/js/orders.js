// =====================================================
// path: static/js/orders.js
// =====================================================
// Por qué: reusar BUY/SELL y el stepper en top y panel lateral.
export function setupOrders({ onOrder, qtyInput, lotValEl, incBtn, decBtn, buyBtns=[], sellBtns=[] }) {
    const clamp2 = v => Math.max(0.01, Math.round(v*100)/100);
    const getQty = () => {
      const fromLot = parseFloat(lotValEl?.textContent || '0.30') || 0.30;
      const fromInput = parseFloat(qtyInput?.value || '0');
      return clamp2(fromInput || fromLot);
    };
    const setQty = (v) => {
      const vv = clamp2(v);
      if (qtyInput) qtyInput.value = vv.toFixed(2);
      if (lotValEl) lotValEl.textContent = vv.toFixed(2);
    };
  
    incBtn?.addEventListener('click', ()=> setQty(getQty()+0.01));
    decBtn?.addEventListener('click', ()=> setQty(getQty()-0.01));
    for (const b of buyBtns)  b?.addEventListener('click', ()=> onOrder?.('buy', getQty()));
    for (const b of sellBtns) b?.addEventListener('click', ()=> onOrder?.('sell', getQty()));
  
    return { getQty, setQty };
  }