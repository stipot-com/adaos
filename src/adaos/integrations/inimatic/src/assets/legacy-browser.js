(function () {
  var doc = document;
  var body = doc.body || doc.getElementsByTagName('body')[0];
  if (!body) {
    return;
  }

  var appRoot = doc.getElementsByTagName('app-root')[0];
  if (appRoot) {
    appRoot.style.display = 'none';
  }

  var container = doc.createElement('div');
  container.style.cssText = 'min-height:100vh;padding:24px;background:#f7f5ef;color:#1f2933;font:16px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;';
  container.innerHTML =
    '<div style="max-width:680px;margin:10vh auto;background:#ffffff;border:1px solid #d9e2ec;border-radius:16px;padding:24px;box-shadow:0 12px 40px rgba(15,23,42,0.08)">' +
      '<h1 style="margin:0 0 12px;font-size:24px;line-height:1.2">Browser update needed</h1>' +
      '<p style="margin:0 0 12px">This device can reach Inimatic, but its browser is too old for the current web app.</p>' +
      '<p style="margin:0">If you are testing on a SmartTV, the TLS handshake is likely already fine and the remaining issue is frontend compatibility.</p>' +
    '</div>';
  body.insertBefore(container, body.firstChild);
})();
