// Dark-mode toggle, shared across every dashboard page. The theme itself
// is applied by a tiny inline script in each page's <head> (before this
// file loads, so there's no flash of the wrong theme) - this just renders
// the toggle button and persists the choice.
//
// Charts derive their grid/tick colors from the resolved theme once at
// page load (see app.js/ist.js's `dark` constant) - rather than rewiring
// every live Chart.js instance to re-theme in place, toggling just
// reloads the page. Simpler, and a theme switch is rare enough that one
// reload is a non-issue.

function resolvedTheme() {
  const explicit = document.documentElement.getAttribute('data-theme');
  if (explicit) return explicit;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function initThemeToggle() {
  const meta = document.querySelector('.topbar-meta');
  if (!meta || document.getElementById('theme-toggle')) return;

  const btn = document.createElement('button');
  btn.id = 'theme-toggle';
  btn.type = 'button';
  btn.className = 'pill pill-muted';
  btn.textContent = resolvedTheme() === 'dark' ? '🌙 Dark' : '☀️ Light';
  btn.title = 'Switch between light and dark theme';
  btn.addEventListener('click', () => {
    const next = resolvedTheme() === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('theme', next); } catch (e) { /* private window etc - just won't persist */ }
    window.location.reload();
  });
  meta.prepend(btn);
}

initThemeToggle();
