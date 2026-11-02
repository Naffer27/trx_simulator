(() => {
  'use strict';
  const root = document.querySelector('[data-passkeys]');
  if (!root) return;
  const status = root.querySelector('[role=status]');
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  function decode(value) {
    return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  }
  function encode(value) {
    if (!value) return null;
    return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  async function post(url, body, isJSON = false) {
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': csrf, 'Accept': 'application/json',
        'Content-Type': isJSON ? 'application/json' : 'application/x-www-form-urlencoded'},
      body: isJSON ? JSON.stringify(body) : new URLSearchParams(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Vuelve a iniciar sesión.');
    return data;
  }
  root.addEventListener('click', async event => {
    const button = event.target.closest('[data-purpose]');
    if (!button || button.disabled) return;
    event.preventDefault();
    button.disabled = true;
    status.textContent = '';
    const form = root.querySelector('form');
    const fields = form ? Object.fromEntries(new FormData(form)) : {};
    const purpose = button.dataset.purpose;
    try {
      if (purpose === 'revoke') {
        const data = await post(root.dataset.revoke, {...fields, key_id: button.dataset.key});
        location.assign(data.redirect); return;
      }
      if (!window.PublicKeyCredential || !navigator.credentials || !window.isSecureContext)
        throw new Error('Este navegador no permite passkeys aquí. Usa tu contraseña y 2FA.');
      const data = await post(root.dataset.options, {...fields, purpose});
      const options = data.options;
      options.challenge = decode(options.challenge);
      if (options.user) options.user.id = decode(options.user.id);
      ['excludeCredentials', 'allowCredentials'].forEach(name => {
        if (options[name]) options[name].forEach(item => { item.id = decode(item.id); });
      });
      const credential = purpose === 'register'
        ? await navigator.credentials.create({publicKey: options})
        : await navigator.credentials.get({publicKey: options});
      const r = credential.response;
      const serialized = {id: credential.id, rawId: encode(credential.rawId), type: credential.type,
        response: {clientDataJSON: encode(r.clientDataJSON)}, clientExtensionResults: credential.getClientExtensionResults()};
      if (r.attestationObject) {
        serialized.response.attestationObject = encode(r.attestationObject);
        serialized.response.transports = r.getTransports ? r.getTransports() : [];
      } else {
        serialized.response.authenticatorData = encode(r.authenticatorData);
        serialized.response.signature = encode(r.signature);
        serialized.response.userHandle = encode(r.userHandle);
      }
      const result = await post(root.dataset.verify, {token: data.token, credential: serialized, name: fields.name}, true);
      location.assign(result.redirect);
    } catch (error) {
      status.textContent = error.name === 'NotAllowedError'
        ? 'No se completó la identificación. Puedes volver a intentarlo o usar tu contraseña.'
        : (error.message || 'No se pudo completar la identificación.');
    } finally {
      button.disabled = false;
      if (form) form.querySelectorAll('input[type=password], input[name=otp_code]').forEach(input => { input.value = ''; });
    }
  });
})();
