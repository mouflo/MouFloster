/* MouFlux · outils communs de la page ⚙️ Réglages (fichier identique dans les trois dépôts) */
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
async function api(url, opts) {
    const res = await fetch(url, opts); let data = {};
    if (res.status === 401) { location.href = '/login'; throw new Error('Session expirée'); }
    try { data = await res.json(); } catch (e) {}
    if (!res.ok && !data.error) data.error = 'Erreur ' + res.status;
    return data;
}
const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
const say = (id, text, ok) => { $(id).innerHTML = text ? `<div class="note ${ok === 'warn' ? 'warn' : ok ? 'ok' : 'err'}">${esc(text)}</div>` : ''; };

// formulaire d'une clé API : Tester / Enregistrer
function keyForm(prefix, endpoint) {
    const state = async () => { const s = await api(endpoint); $(prefix + 'KeyState').textContent = s.configured ? 'Clé actuelle : ' + (s.hint || 'définie') : 'Aucune clé définie'; };
    $(prefix + 'KeyTest').addEventListener('click', async () => { say(prefix + 'KeyMsg', 'Test…', true); const r = await post(endpoint, {api_key: $(prefix + 'Key').value, test: true}); say(prefix + 'KeyMsg', r.message || r.error, r.ok); });
    $(prefix + 'KeySave').addEventListener('click', async () => {
        say(prefix + 'KeyMsg', 'Vérification…', true); const r = await post(endpoint, {api_key: $(prefix + 'Key').value});
        say(prefix + 'KeyMsg', r.message || r.error, r.ok); if (r.ok) { $(prefix + 'Key').value = ''; state(); }
    });
    return state();
}


// formulaire « un nombre » ou « un texte » enregistré par une route POST
function simpleSave(btnId, msgId, url, bodyFn, after) {
    $(btnId).addEventListener('click', async () => { const r = await post(url, bodyFn()); say(msgId, r.message || r.error, r.ok); if (r.ok && after) after(r); });
}

// explorateur de dossiers du serveur : browseFolder(départ, quandChoisi) ; tout bouton <button data-browse="idDuChamp"> remplit ce champ
let _browse = null;
function _browseInit() {
    if (_browse) return _browse;
    const bg = document.createElement('div'); bg.className = 'modalbg';
    bg.innerHTML = '<div class="modalbox"><h2 style="margin:0">📂 Choisir un dossier</h2><div class="cur">/</div><div class="dirs"></div><div class="state"></div>' +
        '<div class="row"><button class="btn ghost" data-a="up">⬆ Remonter</button><button class="btn" data-a="pick">✅ Choisir ce dossier</button><button class="btn ghost" data-a="cancel">Annuler</button></div></div>';
    document.body.appendChild(bg);
    const st = {bg, cur: bg.querySelector('.cur'), dirs: bg.querySelector('.dirs'), note: bg.querySelector('.state'), up: bg.querySelector('[data-a=up]'), path: '/', cb: null};
    async function go(path) {
        const r = await api('/api/fs/list?path=' + encodeURIComponent(path));
        if (r.error) { st.note.textContent = r.error; return; }
        st.path = r.path; st.cur.textContent = r.path; st.note.textContent = r.note || (r.dirs.length ? '' : 'Aucun sous-dossier ici.');
        st.dirs.innerHTML = r.dirs.map(n => `<div data-n="${esc(n)}">📁 ${esc(n)}</div>`).join('');
        st.up.disabled = !r.parent; st.up.dataset.parent = r.parent || '';
    }
    st.go = go;
    st.dirs.addEventListener('click', e => { const d = e.target.closest('[data-n]'); if (d) go(st.path.replace(/\/$/, '') + '/' + d.dataset.n); });
    st.up.addEventListener('click', () => go(st.up.dataset.parent || '/'));
    bg.querySelector('[data-a=cancel]').addEventListener('click', () => bg.classList.remove('show'));
    bg.querySelector('[data-a=pick]').addEventListener('click', () => { bg.classList.remove('show'); if (st.cb) st.cb(st.path); });
    return (_browse = st);
}
function browseFolder(start, cb) { const st = _browseInit(); st.cb = cb; st.bg.classList.add('show'); st.go(start || '/mnt'); }
document.addEventListener('click', e => {
    const b = e.target.closest('[data-browse]'); if (!b) return;
    const input = $(b.dataset.browse); browseFolder(input && input.value || '/mnt', path => { if (input) input.value = path; });
});
