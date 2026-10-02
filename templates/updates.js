/* App updates have their own state; polling never replaces unsaved settings. */
(() => {
  const busyPhases = new Set(['queued', 'checking', 'installing', 'building', 'restarting', 'rolling_back']);
  const el = id => document.getElementById(id);
  let state = null;
  let submitting = false;
  let disconnectedSince = null;

  function render() {
    if (!state) return;
    const busy = submitting || busyPhases.has(state.phase);
    el('update-version').textContent = `Running v${state.running_version} · ${(state.running_revision || 'unknown').slice(0, 12)}`
      + (state.branch ? ` · ${state.remote}/${state.branch}` : '');
    el('update-status').textContent = !state.enabled ? 'One-time setup is required. Open the setup instructions below to enable updates and restarts here.'
      : !state.online ? 'The host updater is offline. Start it on your Docker host to continue.'
      : (state.message || 'Ready to check for updates.');
    el('update-check').disabled = busy || !state.online;
    el('update-install').disabled = busy || !state.online || !state.can_install || state.refresh_running;
    el('update-install').textContent = state.refresh_running ? 'Waiting for refresh' : 'Install update';
    el('update-setup').hidden = state.enabled && state.online;
    el('update-checked').textContent = state.checked_at ? `Last checked ${new Date(state.checked_at * 1000).toLocaleString()}` : '';
    el('update-changes').hidden = !(state.commits || []).length;
    const items = (state.commits || []).map(commit => {
      const item = document.createElement('li');
      item.textContent = commit;
      return item;
    });
    el('update-commits').replaceChildren(...items);
  }

  async function poll() {
    try {
      const response = await fetch('/api/updates', {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('Status unavailable');
      state = await response.json();
      disconnectedSince = null;
      render();
    } catch (_) {
      disconnectedSince ??= Date.now();
      el('update-check').disabled = true;
      el('update-install').disabled = true;
      el('update-status').textContent = Date.now() - disconnectedSince < 180000 && busyPhases.has(state?.phase)
        ? 'Waiting for the app to reconnect after the update…'
        : 'Cannot reach the app. Check the Docker host; this page will keep trying.';
    }
    window.setTimeout(poll, state?.online || disconnectedSince ? 3000 : 15000);
  }

  async function submit(action) {
    if (submitting || !state?.online) return;
    if (action === 'install' && !window.confirm(
      `Install revision ${state.available_revision?.slice(0, 12)} from ${state.branch}? The app will restart and active streams will disconnect. Your saved data and settings will be kept.`
    )) return;
    submitting = true;
    render();
    el('update-error').classList.remove('visible');
    try {
      const response = await fetch(`/api/updates/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Xsort-Update': '1'},
        body: JSON.stringify(action === 'install' ? {revision: state.available_revision} : {}),
        signal: AbortSignal.timeout(10000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The update request failed.');
      state = {...state, phase: 'queued', can_install: false, message: action === 'check' ? 'Update check queued.' : 'Installation queued.'};
    } catch (error) {
      el('update-error').textContent = error.message;
      el('update-error').classList.add('visible');
    } finally {
      submitting = false;
      render();
    }
  }
  el('update-check').addEventListener('click', () => submit('check'));
  el('update-install').addEventListener('click', () => submit('install'));
  poll();
})();
