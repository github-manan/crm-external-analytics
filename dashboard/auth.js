// Shared login gate + "logged in as / Logout" pill for every dashboard
// page. The server already redirects an unauthenticated page load to
// login.html (see serve.py) - this is the client-side half: it renders
// who's logged in, and catches the case where a session expires while
// the page is already open (next /api/auth/me call below 401s).
//
// Actual data scoping (a non-admin only ever seeing their own CRM data)
// happens server-side in serve.py/chatbot.py - nothing here is a security
// boundary, it's just UI.

async function requireSession() {
  let session;
  try {
    const res = await fetch('/api/auth/me');
    if (!res.ok) {
      window.location.href = 'login.html';
      return null;
    }
    session = await res.json();
  } catch (e) {
    window.location.href = 'login.html';
    return null;
  }

  const meta = document.querySelector('.topbar-meta');
  if (meta && !document.getElementById('auth-who')) {
    const who = document.createElement('span');
    who.id = 'auth-who';
    who.className = 'pill pill-muted';
    who.textContent = session.is_admin ? `${session.display_name} · Admin` : session.display_name;

    const logout = document.createElement('button');
    logout.id = 'auth-logout';
    logout.className = 'pill pill-warn';
    logout.type = 'button';
    logout.textContent = 'Logout';
    logout.addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      window.location.href = 'login.html';
    });

    meta.prepend(logout);
    meta.prepend(who);
  }
  return session;
}
