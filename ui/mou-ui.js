/* MouFlux · en-tête, fenêtre Journal et fenêtre « Clé Emby » communs aux applis.
   Page : <div id="mou-header" data-app="mouflopening" data-prefix="MouFl" data-rest="opening"
               data-sub="…" data-version="…" data-emby="1" data-actions='[{"label":"…","url":"/api/…"}]'></div>  */
(function () {
  'use strict';
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var $ = function (id) { return document.getElementById(id); };

  async function api(url, opts) {
    var res = await fetch(url, opts), data = {};
    if (res.status === 401) { location.href = '/login'; throw new Error('Session expirée'); }
    try { data = await res.json(); } catch (e) {}
    if (!res.ok && !data.error) data.error = 'Erreur ' + res.status;
    return data;
  }
  function post(url, body) { return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}); }
  function modal(id, html, narrow) {
    var m = document.createElement('div'); m.className = 'mou-modal'; m.id = id;
    m.innerHTML = '<div class="mou-box' + (narrow ? ' narrow' : '') + '">' + html + '</div>';
    document.body.appendChild(m);
    m.addEventListener('mousedown', function (e) { if (e.target === m) close(m); });
    return m;
  }
  function open(m) { m.classList.add('show'); document.body.style.overflow = 'hidden'; }
  function close(m) { m.classList.remove('show'); if (!document.querySelector('.mou-modal.show')) document.body.style.overflow = ''; }
  window.MouModalOpen = function () { return !!document.querySelector('.mou-modal.show'); };

  function build(host) {
    var d = host.dataset, app = d.app || 'mouflopening';
    var actions = []; try { actions = JSON.parse(d.actions || '[]'); } catch (e) {}
    document.body.classList.add('mou-skin');
    var header = document.createElement('header'); header.className = 'mou-header';
    header.innerHTML =
      '<div class="mou-actions">' + (d.version ? '<span class="mou-ver">' + esc(d.version) + '</span>' : '') +
      (d.emby ? '<button type="button" class="mou-btn ghost small" id="mouKeyOpen">🔑 Clé Emby</button>' : '') +
      '<button type="button" class="mou-btn ghost small" id="mouLogOpen">🩺 Journal</button>' +
      '<form method="post" action="/logout"><button type="submit" class="mou-btn ghost small">Se déconnecter</button></form></div>' +
      '<h1 class="mou-title"><img src="/icons/' + esc(app) + '.svg" alt=""><span><span class="w">' + esc(d.prefix || 'MouFl') + '</span><span class="g">' + esc(d.rest || '') + '</span></span></h1>' +
      (d.sub ? '<p class="mou-sub">' + esc(d.sub) + '</p>' : '');
    host.replaceWith(header);

    // ----- Journal -----
    var extra = actions.map(function (a, i) { return '<button class="mou-btn ghost" data-extra="' + i + '">' + esc(a.label) + '</button>'; }).join('');
    var log = modal('mouLogModal',
      '<h3>🩺 Journal et diagnostic</h3>' +
      '<div class="mou-msub">État du serveur, réglages et dernières lignes du journal. Les clés sont masquées. Copie tout et colle-le dans la conversation.</div>' +
      '<textarea id="mouLogText" readonly spellcheck="false"></textarea><div id="mouLogStatus" class="mou-note" style="display:none"></div>' +
      '<div class="mou-actions-row"><button class="mou-btn" id="mouLogCopy">📋 Copier</button><button class="mou-btn ghost" id="mouLogRefresh">↻ Rafraîchir</button>' + extra +
      '<button class="mou-btn ghost" id="mouLogClose">Fermer</button></div>');
    function status(msg, kind) { var el = $('mouLogStatus'); el.style.display = msg ? 'block' : 'none'; el.textContent = msg || ''; el.className = 'mou-note ' + (kind || 'ok'); }
    async function loadLog() {
      $('mouLogText').value = 'Chargement...';
      try { var r = await api('/api/diagnostic'); $('mouLogText').value = r.report || r.error || '(vide)'; $('mouLogText').scrollTop = $('mouLogText').scrollHeight; }
      catch (e) { $('mouLogText').value = 'Impossible de charger le rapport : ' + e.message; }
    }
    $('mouLogOpen').addEventListener('click', function () { status(''); open(log); loadLog(); });
    $('mouLogClose').addEventListener('click', function () { close(log); });
    $('mouLogRefresh').addEventListener('click', function () { status(''); loadLog(); });
    $('mouLogCopy').addEventListener('click', async function () {
      var box = $('mouLogText'), ok = false;
      try { await navigator.clipboard.writeText(box.value); ok = true; }
      catch (e) { box.focus(); box.select(); try { ok = document.execCommand('copy'); } catch (e2) {} }
      status(ok ? '✅ Copié : colle-le maintenant dans la conversation.' : '⚠️ Copie automatique impossible : le texte est sélectionné, fais « Copier » à la main.', ok ? 'ok' : 'warn');
    });
    log.querySelectorAll('[data-extra]').forEach(function (b) {
      b.addEventListener('click', async function () {
        var a = actions[b.dataset.extra]; if (a.confirm && !confirm(a.confirm)) return;
        var r = await post(a.url); status(r.message || r.error || 'Fait', r.error ? 'err' : 'ok');
      });
    });

    // ----- Clé Emby -----
    if (d.emby) {
      var key = modal('mouKeyModal',
        '<h3>🔑 Clé API Emby</h3><div class="mou-msub" id="mouKeyState">…</div>' +
        '<input id="mouKeyInput" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Colle la nouvelle clé ici">' +
        '<div class="mou-status" id="mouKeyMsg"></div>' +
        '<div class="mou-actions-row"><button class="mou-btn ghost" id="mouKeyTest">Tester</button><button class="mou-btn" id="mouKeySave">Enregistrer</button><button class="mou-btn ghost" id="mouKeyClose">Fermer</button></div>', true);
      var msg = function (t, ok) { $('mouKeyMsg').textContent = t || ''; $('mouKeyMsg').style.color = ok ? '#7fd477' : '#ff9b94'; };
      $('mouKeyOpen').addEventListener('click', async function () {
        msg(''); $('mouKeyInput').value = ''; open(key);
        try { var s = await api('/api/settings/emby'); $('mouKeyState').textContent = (s.configured ? 'Clé actuelle : ' + (s.hint || 'définie') : 'Aucune clé définie') + ' · Emby : ' + (s.host || '?'); } catch (e) { $('mouKeyState').textContent = ''; }
        $('mouKeyInput').focus();
      });
      $('mouKeyTest').addEventListener('click', async function () { msg('Test…', true); var r = await post('/api/settings/emby', {api_key: $('mouKeyInput').value, test: true}); msg(r.message || r.error, r.ok); });
      $('mouKeySave').addEventListener('click', async function () {
        msg('Vérification…', true); var r = await post('/api/settings/emby', {api_key: $('mouKeyInput').value}); msg(r.message || r.error, r.ok);
        if (r.ok) { $('mouKeyInput').value = ''; setTimeout(function () { close(key); }, 1800); }
      });
      $('mouKeyClose').addEventListener('click', function () { close(key); });
    }
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') document.querySelectorAll('.mou-modal.show').forEach(close); });
  }

  // erreurs JavaScript -> journal du serveur
  var n = 0;
  function report(message, where) { if (n++ >= 5) return; fetch('/api/clientlog', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: String(message), where: where || ''})}).catch(function () {}); }
  window.addEventListener('error', function (e) { report(e.message, (e.filename || '') + ':' + (e.lineno || '')); });
  window.addEventListener('unhandledrejection', function (e) { report('Promesse rejetée : ' + (e.reason && e.reason.message || e.reason), ''); });

  function init() { var host = $('mou-header'); if (host) build(host); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
