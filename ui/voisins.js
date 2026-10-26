// 🔗 Applis voisines dans ⚙️ Réglages (fichier identique dans MouFloster et MouFlopening, voir voisins.py) :
// reprendre les réglages communs d'une autre appli MouFl du même serveur, et la clé qui permet aux applis de se parler.
(function () {
    const boite = document.getElementById('voisinesPanel');
    if (!boite) return;
    boite.innerHTML = `<h2>🔗 Applis voisines</h2>
        <div class="sub">Les applis MouFl d'un même serveur se parlent toutes seules (générique cherché après un poster, compte rendu commun de la nuit…). Ici, tu peux aussi reprendre les réglages d'une autre appli au lieu de les retaper.</div>
        <div id="voisReprendre"></div>
        <div id="voisReprendreMsg"></div>
        <details style="margin-top:12px"><summary style="cursor:pointer;color:var(--muted)">🔑 Clé des applis voisines</summary>
            <div class="tabsinfo" id="voisCle" style="margin-top:8px"></div>
            <div class="row" style="margin-top:8px"><button class="btn ghost" id="voisNouvelle">Créer une nouvelle clé</button></div>
            <div id="voisCleMsg"></div>
            <div id="voisDistantes"></div>
        </details>`;

    async function charger() {
        const r = await api('/api/settings/voisines');
        if (r.error) return say('voisReprendreMsg', r.error, false);
        const ici = (r.voisines || []).filter(v => v.ici && v.reglages.length);
        $('voisReprendre').innerHTML = ici.length ? `<div class="field"><label for="voisApp">📥 Reprendre les réglages de</label>
            <select id="voisApp">${ici.map(v => `<option value="${esc(v.id)}">${esc(v.nom)}</option>`).join('')}</select></div>
            <div id="voisListe"></div>
            <div class="row"><button class="btn" id="voisGo">📥 Reprendre les réglages cochés</button></div>`
            : '<div class="state">Aucune autre appli MouFl avec des réglages à reprendre sur ce serveur.</div>';
        const liste = () => {
            const v = ici.find(x => x.id === $('voisApp').value);
            $('voisListe').innerHTML = v.reglages.map(g => `<label style="display:flex;gap:10px;align-items:flex-start;margin:8px 0;cursor:pointer">
                <input type="checkbox" data-nom="${esc(g.nom)}" ${g.pareil ? 'disabled' : (g.ici ? '' : 'checked')} style="width:20px;height:20px;flex:none;margin-top:2px">
                <span>${esc(g.libelle)} : <b>${esc(g.apercu)}</b><br><small style="color:var(--muted)">${g.pareil ? 'déjà le même ici' : g.ici ? 'ici : ' + esc(g.ici) + ' (sera remplacé)' : 'pas encore réglé ici'}</small></span></label>`).join('');
        };
        if (ici.length) {
            liste();
            $('voisApp').addEventListener('change', liste);
            $('voisGo').addEventListener('click', async () => {
                const noms = [...document.querySelectorAll('#voisListe input:checked')].map(c => c.dataset.nom);
                if (!noms.length) return say('voisReprendreMsg', 'Coche au moins un réglage.', false);
                const res = await post('/api/settings/voisines', {action: 'reprendre', app: $('voisApp').value, noms});
                say('voisReprendreMsg', res.message || res.error, res.ok);
                if (res.ok) setTimeout(() => location.reload(), 1200);
            });
        }
        $('voisCle').innerHTML = `Clé de ${esc(r.moi)} : <b>${esc(r.cle_fin || 'pas encore créée (elle l’est au prochain démarrage)')}</b>. Les applis du même serveur la lisent toutes seules, il n’y a rien à recopier. Une nouvelle clé ne sert que si une voisine est installée sur une autre machine (elle te sera montrée une seule fois).`;
        const loin = (r.voisines || []).filter(v => ['moufloster', 'mouflopening'].includes(v.id));
        $('voisDistantes').innerHTML = loin.map(v => `<div class="field" style="margin-top:12px"><label for="voisCle_${esc(v.id)}">${esc(v.nom)} : ${v.ici ? '✅ sur ce serveur (rien à régler)' : v.reliee ? '✅ relié (autre machine)' : 'clé de ' + esc(v.nom) + ' s’il est sur une autre machine'}</label>
            ${v.ici ? '' : `<div class="row nowrap"><input type="password" id="voisCle_${esc(v.id)}" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="${v.cle_reglee ? '(inchangée)' : 'colle sa clé ici'}"><button class="btn ghost" data-distante="${esc(v.id)}">Enregistrer</button></div>`}</div>`).join('');
        document.querySelectorAll('[data-distante]').forEach(b => b.addEventListener('click', async () => {
            const res = await post('/api/settings/voisines', {action: 'distante', app: b.dataset.distante, cle: $('voisCle_' + b.dataset.distante).value});
            say('voisCleMsg', res.message || res.error, res.ok); if (res.ok) charger();
        }));
    }
    $('voisNouvelle').addEventListener('click', async () => {
        if (!confirm('Créer une nouvelle clé ? Une voisine installée sur une autre machine devra recevoir la nouvelle (celles du même serveur la liront toutes seules).')) return;
        const res = await post('/api/settings/voisines', {action: 'nouvelle_cle'});
        if (!res.ok) return say('voisCleMsg', res.error, false);
        $('voisCleMsg').innerHTML = `<div class="note ok">${esc(res.message)}<br><code style="user-select:all;word-break:break-all">${esc(res.cle)}</code></div>`;
        charger();
    });
    charger();
})();
