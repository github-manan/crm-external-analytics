// Per-card refresh button, shared by the dashboard pages. Clicking it
// re-runs that card's own loader - a fresh read of the local database,
// which is only as new as the last Zoho sync (scripts/extract.py); it
// doesn't pull from Zoho itself.

const REFRESH_ICON = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
  <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M12.5 1.8v2.9H9.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

// `target` is the card element (or any element inside it); `loader` is an
// async function that reloads the card. The button goes into the card's
// .card-head, or the card's top-right corner when it has none (KPI tiles).
function addRefresh(target, loader, label) {
  const card = target.closest('.card, .kpi-card') || target;
  if (card.querySelector(':scope > .refresh-btn, :scope > .card-head > .refresh-btn')) return;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'refresh-btn';
  btn.innerHTML = REFRESH_ICON;
  const name = label || card.querySelector('h2, .kpi-label')?.textContent?.trim() || 'this card';
  btn.setAttribute('aria-label', `Refresh ${name}`);
  btn.title = `Refresh ${name}`;

  btn.addEventListener('click', async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.remove('failed');
    btn.classList.add('spinning');
    try {
      await loader();
      btn.title = `Refreshed ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
    } catch (e) {
      if (e?.name === 'AbortError') return; // a filter change superseded it
      console.error(e);
      btn.classList.add('failed');
      btn.title = 'Refresh failed - see console';
    } finally {
      btn.classList.remove('spinning');
      btn.disabled = false;
    }
  });

  const head = card.querySelector(':scope > .card-head');
  if (head) {
    head.appendChild(btn);
  } else {
    card.classList.add('has-refresh');
    card.appendChild(btn);
  }
}
