(async () => {
  const base = document.querySelector('meta[name="iib-base"]').content;
  const ticket = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  try {
    const response = await fetch(base + '/desktop-session', { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ticket}) });
    if (!response.ok) throw new Error();
    location.replace(base + '/library');
  } catch { document.getElementById('empty').textContent = '起動リンクの有効期限が切れました。run.batを開き直してください。'; }
})();
