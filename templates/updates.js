/* App updates have their own state; polling never replaces unsaved settings. */
(() => {
  const busyPhases = new Set(['queued', 'checking', 'waiting_for_idle', 'installing', 'building', 'restarting', 'rolling_back']);
  const el = id => document.getElementById(id);
  let state = null;
  let submitting = false;
  let disconnectedSince = null;

  function render() {
    if (!state) return;
    const busy = submitting || busyPhases.has(state.phase);
    const blockers = state.idle_blockers || [];
    const waiting = state.phase === 'waiting_for_idle';
    el('update-version').textContent = `Running v${state.running_version} · ${(state.running_revision || 'unknown').slice(0, 12)}`
      + (state.branch ? ` · ${state.remote}/${state.branch}` : '');
    el('update-status').textContent = !state.enabled ? 'One-time setup is required. Open the setup instructions below to enable updates and restarts here.'
      : !state.online ? 'The host updater is offline. Start it on your Docker host to continue.'
      : (state.message || 'Ready to check for updates.') + (state.can_install && blockers.length
          ? ` Active: ${blockers.join(', ')}. You can schedule this update for when the system is idle.` : '');
    el('update-check').disabled = busy || !state.online;
    el('update-install').disabled = busy || !state.online || !state.can_install;
    el('update-install').textContent = blockers.length ? 'Install when system is idle' : 'Install update';
    el('update-cancel-waiting').hidden = !waiting;
    el('update-cancel-waiting').disabled = submitting || !state.online || !!state.cancel_requested;
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
    if (action === 'install' && (state.idle_blockers || []).length) action = 'install_when_idle';
    if (['install', 'install_when_idle'].includes(action) && !window.confirm(action === 'install'
      ? `Install revision ${state.available_revision?.slice(0, 12)} from ${state.branch}? The app will restart. Your saved data and settings will be kept.`
      : `Schedule revision ${state.available_revision?.slice(0, 12)} from ${state.branch}? It will install automatically once downloads, streams, and refreshes are idle.`)) return;
    submitting = true;
    render();
    el('update-error').classList.remove('visible');
    try {
      const response = await fetch(`/api/updates/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Xsort-Update': '1'},
        body: JSON.stringify(['install', 'install_when_idle'].includes(action) ? {revision: state.available_revision} : {}),
        signal: AbortSignal.timeout(10000),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'The update request failed.');
      state = {...state, phase: 'queued', can_install: false, message: action === 'check' ? 'Update check queued.'
        : action === 'cancel_waiting' ? 'Cancelling scheduled installation…'
        : action === 'install_when_idle' ? 'Scheduling installation for when the system is idle…' : 'Installation queued.'};
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
  el('update-cancel-waiting').addEventListener('click', () => submit('cancel_waiting'));
  poll();
})();
