/* 🎨 Apparence : choix d'un skin et des couleurs, par compte.
   Ajoute un bouton dans l'en-tête commun sans toucher au kit partagé (ui/mou-ui.js). */
(function () {
  'use strict';
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); };
  var $ = function (id) { return document.getElementById(id); };
  var etat = null, modale = null;

  async function api(url, corps) {
    var opts = corps === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(corps)};
    var r = await fetch(url, opts), d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok && !d.error) d.error = 'Erreur ' + r.status;
    return d;
  }
  function msg(t, ok) { var m = $('appMsg'); m.textContent = t || ''; m.style.color = ok ? '#7fd477' : '#ff9b94'; }

  function dessiner() {
    var c = etat.choix, skins = etat.skins;
    var liste = '<label class="app-skin"><input type="radio" name="appSkin" value=""' + (!c.skin ? ' checked' : '') + '> <b>Style d\'origine</b><small>Le style habituel de MouFloster.</small></label>' +
      skins.map(function (s) {
        return '<label class="app-skin"><input type="radio" name="appSkin" value="' + esc(s.id) + '"' + (c.skin === s.id ? ' checked' : '') + '> <b>' + esc(s.nom) + '</b>' +
          (s.perso ? ' <span class="app-tag">brouillon</span>' : '') +
          '<small>' + esc(s.description) + (s.auteur ? ' · par ' + esc(s.auteur) : '') + '</small>' +
          (s.perso ? ' <button type="button" class="mou-btn danger small" data-suppr="' + esc(s.id) + '">Supprimer</button>' : '') + '</label>';
      }).join('');
    var cols = Object.keys(etat.defauts).map(function (k) {
      var v = (c.couleurs && c.couleurs[k]) || etat.defauts[k];
      return '<label class="app-col"><span>' + esc(etat.libelles[k] || k) + '</span><input type="color" data-col="' + k + '" value="' + esc(v) + '"></label>';
    }).join('');
    $('appCorps').innerHTML =
      '<div class="mou-msub">Choisis un style, puis ajuste les couleurs si tu veux. Ça ne change que ton affichage.</div>' +
      '<div class="app-liste">' + liste + '</div>' +
      '<h4 style="margin:14px 0 6px">Couleurs</h4><div class="app-cols">' + cols + '</div>' +
      '<div class="mou-actions-row app-barre"><button type="button" class="mou-btn" id="appSave">Enregistrer</button>' +
      '<button type="button" class="mou-btn ghost" id="appCoulReset">Couleurs d\'origine</button>' +
      '<button type="button" class="mou-btn ghost" id="appReset">↩︎ Tout remettre à l\'origine</button></div>' +
      '<details style="margin-top:14px"><summary>🧪 Essayer un skin brouillon (CSS)</summary>' +
      '<div class="mou-msub">Colle le CSS d\'un skin en cours de création. Il n\'est visible que par toi.</div>' +
      '<input type="text" id="appNom" placeholder="Nom du skin" style="width:100%;margin-bottom:6px">' +
      '<textarea id="appCss" class="mou-field" rows="6" spellcheck="false" placeholder="/* CSS du skin */" style="width:100%;font-family:monospace;font-size:16px"></textarea>' +
      '<div class="mou-actions-row"><button type="button" class="mou-btn ghost" id="appBrouillon">Enregistrer le brouillon</button></div></details>' +
      '<div id="appMsg" class="mou-msub"></div>' +
      '<div class="mou-actions-row"><button type="button" class="mou-btn ghost" id="appClose">Fermer</button></div>';
  }

  function lire() {
    var c = {skin: '', couleurs: {}};
    var r = document.querySelector('input[name=appSkin]:checked'); c.skin = r ? r.value : '';
    document.querySelectorAll('[data-col]').forEach(function (i) {
      if (i.value.toLowerCase() !== (etat.defauts[i.dataset.col] || '').toLowerCase() || i.dataset.touche) c.couleurs[i.dataset.col] = i.value;
    });
    return c;
  }
  async function charger() { etat = await api('/api/apparence'); if (etat.error) { msg(etat.error); return false; } dessiner(); return true; }

  async function ouvrir() {
    if (!modale) {
      modale = document.createElement('div'); modale.className = 'mou-modal'; modale.id = 'appModal';
      modale.innerHTML = '<div class="mou-box"><h3>🎨 Apparence</h3><div id="appCorps">Chargement…</div></div>';
      document.body.appendChild(modale);
      modale.addEventListener('mousedown', function (e) { if (e.target === modale) modale.classList.remove('show'); });
      modale.addEventListener('input', function (e) { if (e.target.dataset && e.target.dataset.col) e.target.dataset.touche = '1'; });
      modale.addEventListener('click', async function (e) {
        var t = e.target.closest('button'); if (!t) return;
        if (t.id === 'appClose') modale.classList.remove('show');
        else if (t.id === 'appSave') {
          var r = await api('/api/apparence', lire());
          if (r.ok) location.reload(); else msg(r.error || 'Échec');
        } else if (t.id === 'appCoulReset') {
          var c = lire(); c.couleurs = {};
          var r2 = await api('/api/apparence', c); if (r2.ok) location.reload(); else msg(r2.error || 'Échec');
        } else if (t.id === 'appReset') {
          var r3 = await api('/api/apparence', {reset: true}); if (r3.ok) location.reload(); else msg(r3.error || 'Échec');
        } else if (t.id === 'appBrouillon') {
          var r4 = await api('/api/apparence/brouillon', {nom: $('appNom').value, css: $('appCss').value});
          if (r4.ok) { var r5 = await api('/api/apparence', {skin: r4.id, couleurs: lire().couleurs}); if (r5.ok) location.reload(); else msg(r5.error); }
          else msg(r4.error || 'Échec');
        } else if (t.dataset.suppr) {
          if (!confirm('Supprimer ce brouillon ?')) return;
          await api('/api/apparence/brouillon', {supprimer: t.dataset.suppr}); charger();
        }
      });
      document.addEventListener('keydown', function (e) { if (e.key === 'Escape') modale.classList.remove('show'); });
    }
    modale.classList.add('show');
    await charger();
  }

  function installer(essais) {
    var barre = document.querySelector('.mou-actions');
    if (!barre) { if (essais > 0) setTimeout(function () { installer(essais - 1); }, 100); return; }
    if ($('mouApparence')) return;
    var b = document.createElement('button');
    b.type = 'button'; b.id = 'mouApparence'; b.className = 'mou-btn ghost small'; b.textContent = '🎨 Apparence';
    b.addEventListener('click', ouvrir);
    barre.insertBefore(b, barre.querySelector('#mouSettingsOpen') || barre.firstChild);
  }
  installer(50);
})();
