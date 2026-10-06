/* MouFlux · en-tête, fenêtre Journal et fenêtre « Clé Emby » communs aux applis.
   Page : <div id="mou-header" data-app="mouflopening" data-prefix="MouFl" data-rest="opening"
               data-sub="…" data-version="…" data-emby="1" data-tmdb="1" data-actions='[{"label":"…","url":"/api/…"}]'></div>  */
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
      (d.settings ? '<a class="mou-btn ghost small" href="/reglages" id="mouSettingsOpen" style="text-decoration:none">⚙️ Réglages</a>' : '') +
      (d.emby && !d.settings ? '<button type="button" class="mou-btn ghost small" id="mouKeyOpen">🔑 Clé Emby</button>' : '') +
      (d.tmdb && !d.settings ? '<button type="button" class="mou-btn ghost small" id="mouTmdbOpen">🔑 Clé TMDB</button>' : '') +
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

    // ----- Fenêtres de clés (Emby, TheMovieDB) -----
    function keyModal(name, title, endpoint, openId) {
      var id = 'mouKey' + name;
      var key = modal(id + 'Modal',
        '<h3>' + title + '</h3><div class="mou-msub" id="' + id + 'State">…</div>' +
        '<input id="' + id + 'Input" type="password" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Colle la nouvelle clé ici">' +
        '<div class="mou-status" id="' + id + 'Msg"></div>' +
        '<div class="mou-actions-row"><button class="mou-btn ghost" id="' + id + 'Test">Tester</button><button class="mou-btn" id="' + id + 'Save">Enregistrer</button><button class="mou-btn ghost" id="' + id + 'Close">Fermer</button></div>', true);
      var msg = function (t, ok) { $(id + 'Msg').textContent = t || ''; $(id + 'Msg').style.color = ok ? '#7fd477' : '#ff9b94'; };
      $(openId).addEventListener('click', async function () {
        msg(''); $(id + 'Input').value = ''; open(key);
        try { var s = await api(endpoint); $(id + 'State').textContent = (s.configured ? 'Clé actuelle : ' + (s.hint || 'définie') : 'Aucune clé définie') + (s.host ? ' · Emby : ' + s.host : ''); } catch (e) { $(id + 'State').textContent = ''; }
        $(id + 'Input').focus();
      });
      $(id + 'Test').addEventListener('click', async function () { msg('Test…', true); var r = await post(endpoint, {api_key: $(id + 'Input').value, test: true}); msg(r.message || r.error, r.ok); });
      $(id + 'Save').addEventListener('click', async function () {
        msg('Vérification…', true); var r = await post(endpoint, {api_key: $(id + 'Input').value}); msg(r.message || r.error, r.ok);
        if (r.ok) { $(id + 'Input').value = ''; setTimeout(function () { close(key); }, 1800); }
      });
      $(id + 'Close').addEventListener('click', function () { close(key); });
    }
    if (d.emby && !d.settings) keyModal('Emby', '🔑 Clé API Emby', '/api/settings/emby', 'mouKeyOpen');
    if (d.tmdb && !d.settings) keyModal('Tmdb', '🔑 Clé API TheMovieDB', '/api/settings/tmdb', 'mouTmdbOpen');
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') document.querySelectorAll('.mou-modal.show').forEach(close); });
  }

  // erreurs JavaScript -> journal du serveur
  var n = 0;
  function report(message, where) { if (n++ >= 5) return; fetch('/api/clientlog', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: String(message), where: where || ''})}).catch(function () {}); }
  window.addEventListener('error', function (e) { report(e.message, (e.filename || '') + ':' + (e.lineno || '')); });
  window.addEventListener('unhandledrejection', function (e) { report('Promesse rejetée : ' + (e.reason && e.reason.message || e.reason), ''); });

  // ----- Messages toujours visibles -----
  // Quand un message apparaît hors de l'écran (en haut ou en bas de la page) juste après un appui,
  // il est aussi montré dans une bulle en bas de l'écran. MouToast(texte, 'ok' | 'err' | 'warn') pour en afficher une soi-même.
  var ZONES = '.note, .mou-note, .message, .lib-status';
  var dernierGeste = 0, recents = {}, pile = null;
  ['click', 'keydown', 'change', 'submit', 'touchend'].forEach(function (t) {
    document.addEventListener(t, function () { dernierGeste = Date.now(); }, true);
  });
  function toast(texte, genre) {
    texte = String(texte || '').trim();
    if (!texte || Date.now() - (recents[texte] || 0) < 2500) return;
    recents[texte] = Date.now();
    if (!pile) { pile = document.createElement('div'); pile.className = 'mou-toasts'; pile.setAttribute('aria-live', 'polite'); document.body.appendChild(pile); }
    var b = document.createElement('div');
    b.className = 'mou-toast ' + (genre || 'ok'); b.setAttribute('role', 'status');
    b.textContent = texte.length > 280 ? texte.slice(0, 277) + '…' : texte;
    var fermer = function () { b.classList.add('fin'); setTimeout(function () { b.remove(); }, 250); };
    b.addEventListener('click', fermer);
    pile.appendChild(b);
    while (pile.children.length > 3) pile.firstChild.remove();
    setTimeout(fermer, genre === 'ok' ? 4500 : 8000);
  }
  window.MouToast = toast;
  function genreDe(el) {
    var c = ' ' + el.className + ' ' + (el.firstElementChild ? el.firstElementChild.className : '') + ' ';
    return / (err|error|danger) /.test(c) ? 'err' : / warn /.test(c) ? 'warn' : 'ok';
  }
  function horsEcran(el) {
    var r = el.getBoundingClientRect();
    if (!r.width && !r.height) return false;                        // caché : rien à signaler
    return r.top < 0 || r.bottom > (window.innerHeight || document.documentElement.clientHeight);
  }
  var aVerifier = new Set(), minuterie = null;
  function verifier() {
    minuterie = null;
    if (Date.now() - dernierGeste > 15000) { aVerifier.clear(); return; }   // seulement en réponse à une action
    aVerifier.forEach(function (el) {
      var texte = (el.innerText || el.textContent || '').trim();
      if (!texte || !document.body.contains(el) || !horsEcran(el)) return;
      if (el._mouGeste === dernierGeste && genreDe(el) !== 'err') return;   // une bulle par zone et par appui
      el._mouGeste = dernierGeste;
      toast(texte, genreDe(el));
    });
    aVerifier.clear();
  }
  function surveiller() {
    new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        var cible = m.target.nodeType === 1 ? m.target : m.target.parentElement;
        var zone = cible && cible.closest && cible.closest(ZONES);
        if (zone) aVerifier.add(zone);
        m.addedNodes && m.addedNodes.forEach(function (n) {
          if (n.nodeType !== 1) return;
          if (n.matches(ZONES)) aVerifier.add(n);
          n.querySelectorAll && n.querySelectorAll(ZONES).forEach(function (z) { aVerifier.add(z); });
        });
      });
      if (aVerifier.size && !minuterie) minuterie = setTimeout(verifier, 60);
    }).observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class']});
  }

  function init() { var host = $('mou-header'); if (host) build(host); surveiller(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
