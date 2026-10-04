// Login page only - posts to /api/auth/login, which sets the session
// cookie the rest of the dashboard relies on (see auth.js, serve.py).

const form = document.getElementById('login-form');
const errorEl = document.getElementById('login-error');
const submitBtn = document.getElementById('login-submit');

// Already logged in? Skip straight past the login page.
fetch('/api/auth/me').then(res => {
  if (res.ok) window.location.href = 'index.html';
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errorEl.hidden = true;
  submitBtn.disabled = true;
  submitBtn.textContent = 'Signing in…';

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: document.getElementById('username').value.trim(),
        password: document.getElementById('password').value,
      }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      errorEl.textContent = data.error || 'Login failed.';
      errorEl.hidden = false;
      return;
    }
    window.location.href = 'index.html';
  } catch (err) {
    errorEl.textContent = 'Could not reach the server - is it running?';
    errorEl.hidden = false;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Sign in';
  }
});
