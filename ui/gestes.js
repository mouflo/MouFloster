// MouFloster au téléphone : faire défiler la page sans toucher aux réglages par mégarde.
// - Un appui qui arrive pendant que la page défile (le doigt posé pour l'arrêter) est ignoré : aucune case cochée,
//   aucun bouton ni poster choisi sans le vouloir.
// - Les curseurs ne bougent que si on les glisse de côté (jamais d'un simple appui, jamais en défilant) ; boutons − / +.
// - Un petit aperçu reste collé en bas de l'écran tant que le grand aperçu n'est pas visible.
(function () {
    'use strict';
    const maintenant = () => performance.now();

    // ---------- 1. Appui pendant un défilement : ignoré ----------
    let dernierDefilement = -1e9, appuiPendantDefilement = false;
    document.addEventListener('scroll', () => { dernierDefilement = maintenant(); }, {capture: true, passive: true});
    document.addEventListener('touchstart', () => { appuiPendantDefilement = maintenant() - dernierDefilement < 300; }, {capture: true, passive: true});
    document.addEventListener('pointerdown', (e) => { if (e.pointerType !== 'touch') appuiPendantDefilement = false; }, true);
    const SENSIBLES = 'input[type=checkbox], input[type=radio], label, button, a, .poster-thumb, .result-item, img';
    document.addEventListener('click', (e) => {
        if (!appuiPendantDefilement || !e.isTrusted || !e.target.closest(SENSIBLES)) return;
        appuiPendantDefilement = false;
        e.preventDefault();
        e.stopImmediatePropagation();
    }, true);
    window.mouAppuiPendantDefilement = () => appuiPendantDefilement;

    // ---------- 2. Curseurs : glissés de côté seulement, plus − / + ----------
    const PAS = {cropX: 2, cropY: 2, zoom: 5, cadrePx: 1, eraseBrush: 1};
    function changer(r, valeur) {
        const min = Number(r.min || 0), max = Number(r.max || 100), pas = Number(r.step) || 1;
        const v = Math.min(max, Math.max(min, Math.round(valeur / pas) * pas));
        if (v === Number(r.value)) return false;
        r.value = v;
        r.dispatchEvent(new Event('input', {bubbles: true}));
        return true;
    }
    function equiper(r) {
        if (r.closest('.curseur')) return;
        const boite = document.createElement('div');
        boite.className = 'curseur';
        r.parentNode.insertBefore(boite, r);
        const bouton = (texte, signe, aide) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'secondary pas'; b.textContent = texte; b.setAttribute('aria-label', aide);
            b.addEventListener('click', () => { if (changer(r, Number(r.value) + signe * (PAS[r.id] || Number(r.step) || 1))) r.dispatchEvent(new Event('change', {bubbles: true})); });
            return b;
        };
        boite.append(bouton('−', -1, 'Moins'), r, bouton('+', 1, 'Plus'));
        // Au doigt : le curseur lui-même ne reçoit rien (CSS) ; c'est la boîte qui suit le geste.
        // Glisser de côté = déplacer le rond d'autant (pas de saut à l'endroit touché) ; glisser de haut en bas = défilement.
        let g = null;
        boite.addEventListener('pointerdown', (e) => {
            if (e.pointerType !== 'touch' || e.target.closest('button') || appuiPendantDefilement) return;
            g = {id: e.pointerId, x: e.clientX, y: e.clientY, v: Number(r.value), largeur: r.getBoundingClientRect().width || 1, sens: null};
        });
        boite.addEventListener('pointermove', (e) => {
            if (!g || e.pointerId !== g.id) return;
            const dx = e.clientX - g.x, dy = e.clientY - g.y;
            if (!g.sens) {
                if (Math.hypot(dx, dy) < 8) return;
                g.sens = Math.abs(dx) > Math.abs(dy) * 1.2 ? 'cote' : 'defile';
                if (g.sens === 'cote') { try { boite.setPointerCapture(e.pointerId); } catch (x) {} }
            }
            if (g.sens !== 'cote') return;
            e.preventDefault();
            const etendue = Number(r.max || 100) - Number(r.min || 0);
            if (changer(r, g.v + dx / g.largeur * etendue)) g.change = true;
        });
        const fin = (e) => {
            if (!g || e.pointerId !== g.id) return;
            if (g.change) r.dispatchEvent(new Event('change', {bubbles: true}));
            g = null;
        };
        boite.addEventListener('pointerup', fin);
        boite.addEventListener('pointercancel', fin);
    }
    document.querySelectorAll('input[type=range]').forEach(equiper);

    // ---------- 3. Petit aperçu collé en bas (téléphone et tablette) ----------
    const grand = document.getElementById('previewImage');
    const mini = document.getElementById('miniApercu'), retour = document.getElementById('retourEdition');
    if (!grand || !mini || !retour) return;
    const etroit = window.matchMedia('(max-width: 1023px)');
    let grandVisible = true, ouEtais = null;
    const aUnApercu = () => !!grand.querySelector('img');
    function majFlottants() {
        const actif = etroit.matches && aUnApercu();
        mini.hidden = !(actif && !grandVisible && ouEtais === null);
        retour.hidden = !(actif && grandVisible && ouEtais !== null);
    }
    new IntersectionObserver((entrees) => {
        grandVisible = entrees[0].isIntersecting;
        if (!grandVisible) ouEtais = null;          // on a quitté l'aperçu : plus besoin du bouton « retour »
        majFlottants();
    }, {threshold: 0.35}).observe(grand);
    document.addEventListener('apercu', (e) => { mini.querySelector('img').src = e.detail; majFlottants(); });
    new MutationObserver(majFlottants).observe(grand, {childList: true});
    mini.addEventListener('click', () => {
        ouEtais = window.scrollY;
        grand.scrollIntoView({behavior: 'smooth', block: 'center'});
        majFlottants();
    });
    retour.addEventListener('click', () => {
        const y = ouEtais; ouEtais = null;
        window.scrollTo({top: y, behavior: 'smooth'});
        majFlottants();
    });
    etroit.addEventListener('change', majFlottants);
})();
