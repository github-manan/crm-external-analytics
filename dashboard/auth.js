// Who's viewing, for the page's own UI choices (e.g. the rep filter).
// There's no login in this app - access is controlled by the
// internal.datasurfr.ai platform in front of it - so /api/me currently
// returns the same full-access viewer for everyone. Data scoping, if it
// comes back, happens server-side in serve.py/chatbot.py, not here.

async function getViewer() {
  try {
    const res = await fetch('/api/me');
    if (res.ok) return await res.json();
  } catch (e) {
    console.error(e);
  }
  return { display_name: 'Viewer', owner_name: null, is_admin: true };
}
