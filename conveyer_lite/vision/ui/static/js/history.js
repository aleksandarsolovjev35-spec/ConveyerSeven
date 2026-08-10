// history.js — Line Monitor UI module
'use strict';

// ─── Recent parts ────────────────────────────────────────────

function updateRecentParts(parts) {
    const hash = parts.map(p =>
        `${p.id}:${p.category}:${p.decision}`
    ).join('|');

    if (els.historyCards.dataset.hash === hash) {
        updateDefects(parts);
        return;
    }
    els.historyCards.dataset.hash = hash;

    if (!parts.length) {
        els.historyCards.innerHTML =
            '<span class="history-empty">Корпусов пока нет</span>';
        animateUiElement(els.historyCards, 'ui-content-change');
        updateDefects(parts);
        return;
    }

    const visible = parts.slice(-10);

    els.historyCards.innerHTML = visible.map(p => {
        const cat = (p.category || 'GOOD').toLowerCase();
        let symbol = 'ГОДНО';
        if (cat === 'bad')     symbol = 'БРАК';
        if (cat === 'cleanup') symbol = 'ОЧИСТКА';

        return `
            <div class="history-card cat-${cat}"
                 onclick="window._openPartGallery(${p.id})"
                 title="Корпус #${p.id} · ${symbol}"
                 style="cursor:pointer">
                <div class="history-card-id">#${p.id}</div>
                <div class="history-card-symbol">${symbol}</div>
            </div>
        `;
    }).join('');
    animateUiElement(els.historyCards, 'ui-content-change');

    updateDefects(parts);
}

function updateDefects(parts) {
    if (!els.defectsSection || !els.defectsList) return;
    const working = state.lineState === 'RUNNING' || state.lineState === 'STOPPING';
    if (!working) {
        els.defectsSection.classList.add('is-hidden');
        return;
    }

    setIfChanged(els.defectsTitle, 'ОСНОВНЫЕ ДЕФЕКТЫ');
    const rows = [];
    const counter = {};
    for (const part of parts) {
        // Prefer human-readable cause if available in future payloads
        const cause = part.human_cause || part.decision;
        if (!cause || cause === 0 || cause === '0' || cause === 'none' || cause === 'unknown') continue;
        const key = String(cause).toUpperCase().slice(0, 42);
        counter[key] = (counter[key] || 0) + 1;
    }
    for (const [name, count] of Object.entries(counter)
        .sort((left, right) => right[1] - left[1])
        .slice(0, 5)) {
        rows.push({name, value: count});
    }
    if (!rows.length) rows.push({name: '(нет дефектов)', value: '—'});

    const hash = JSON.stringify({working, rows});
    if (els.defectsList.dataset.lastHash !== hash) {
        els.defectsList.dataset.lastHash = hash;
        els.defectsList.replaceChildren();
        for (const row of rows) {
            const item = document.createElement('li');
            if (row.error) item.classList.add('diagnostic-error');
            const name = document.createElement('span');
            const value = document.createElement('b');
            name.textContent = row.name;
            value.textContent = row.value;
            item.append(name, value);
            els.defectsList.appendChild(item);
        }
    }
    els.defectsSection.classList.remove('is-hidden');
}

// ─── Archive Gallery ─────────────────────────────────────────

function setupGallery() {
    if (els.galleryClose) {
        els.galleryClose.addEventListener('click', closeGallery);
    }

    const backdrop = document.querySelector('.gallery-backdrop');
    if (backdrop) {
        backdrop.addEventListener('click', closeGallery);
    }

    if (els.galleryModeDebug) {
        els.galleryModeDebug.addEventListener('click', () => {
            galleryMode = 'debug';
            els.galleryModeDebug.classList.add('active');
            if (els.galleryModeRaw) {
                els.galleryModeRaw.classList.remove('active');
            }
            renderGalleryImages();
        });
    }

    if (els.galleryModeRaw) {
        els.galleryModeRaw.addEventListener('click', () => {
            galleryMode = 'raw';
            if (els.galleryModeDebug) {
                els.galleryModeDebug.classList.remove('active');
            }
            els.galleryModeRaw.classList.add('active');
            renderGalleryImages();
        });
    }
}

async function openGallery(partId) {
    galleryPartId = partId;
    galleryMode   = 'debug';

    if (!els.galleryModal || !els.galleryGrid) return;

    if (els.galleryModeDebug) {
        els.galleryModeDebug.classList.add('active');
    }
    if (els.galleryModeRaw) {
        els.galleryModeRaw.classList.remove('active');
    }

    els.galleryGrid.innerHTML =
        '<div class="gallery-loading">Загрузка...</div>';
    els.galleryModal.classList.remove('is-hidden');

    if (els.galleryPartId) {
        els.galleryPartId.textContent = partId;
    }

    const data = await apiGet(`/api/archive/part/${partId}`);
    if (!data) {
        els.galleryGrid.innerHTML =
            '<div class="gallery-loading">Корпус не найден в архиве</div>';
        return;
    }

    galleryData = data;
    const meta = data.meta || {};

    if (els.galleryCategory) {
        els.galleryCategory.textContent = `КАТЕГОРИЯ: ${categoryLabel(meta.category)}`;
        const cat = (meta.category || '').toLowerCase();

        if (cat === 'good') {
            els.galleryCategory.style.color = 'var(--ok)';
        } else if (cat === 'bad') {
            els.galleryCategory.style.color = 'var(--bad)';
        } else if (cat === 'cleanup') {
            els.galleryCategory.style.color = 'var(--warn)';
        } else {
            els.galleryCategory.style.color = 'var(--text-dim)';
        }
    }

    if (els.galleryDecision) {
        const decision = (!meta.decision || meta.decision === 'none')
            ? 'БЕЗ ДЕФЕКТОВ'
            : meta.decision;
        els.galleryDecision.textContent = `РЕШЕНИЕ: ${decision}`;
    }
    if (els.galleryTime) {
        els.galleryTime.textContent = `ВРЕМЯ: ${meta.time || '—'}`;
    }
    if (els.galleryBatch) {
        els.galleryBatch.textContent = `ПАРТИЯ: ${meta.batch_id || '—'}`;
    }
    if (els.galleryDefects) {
        const defects = meta.defects || [];
        els.galleryDefects.textContent = defects.length
            ? defects.join(', ')
            : 'нет';
        els.galleryDefects.style.color = defects.length
            ? 'var(--bad)'
            : 'var(--ok)';
    }

    renderGalleryImages();
}

function renderGalleryImages() {
    if (!els.galleryGrid || !galleryData) return;

    const roles = galleryData.roles || [];

    if (!roles.length) {
        els.galleryGrid.innerHTML =
            '<div class="gallery-loading">Изображения отсутствуют</div>';
        return;
    }

    const existingCards = els.galleryGrid.querySelectorAll('.gallery-card');

    if (existingCards.length !== roles.length) {
        els.galleryGrid.innerHTML = roles.map(r => {
            const url = getGalleryUrl(r);
            if (!url) return '';

            return `
                <div class="gallery-card" data-role="${r.role}">
                    <div class="gallery-card-label">${cameraRoleLabel(r.role)}</div>
                    <div class="gallery-card-img-wrap">
                        <img class="gallery-img-fade-in"
                             src="${url}"
                             alt="${r.role}"
                             onload="window._galleryImageLoaded(this)"
                             onerror="window._galleryImageError(this)"
                             onclick="window._galleryFullscreen(this.src)">
                    </div>
                </div>
            `;
        }).join('');
        attachGalleryImageErrorHandlers();
        return;
    }

    existingCards.forEach(card => {
        const role     = card.dataset.role;
        const roleData = roles.find(r => r.role === role);
        if (!roleData) return;

        const wrap = card.querySelector('.gallery-card-img-wrap');
        if (!wrap) return;

        const oldImg = wrap.querySelector('img:not(.gallery-img-fade-out)');
        if (!oldImg) return;

        const newUrl = getGalleryUrl(roleData);
        if (!newUrl) return;

        if (oldImg.src.endsWith(newUrl.split('?')[0])) return;

        const ghost = oldImg.cloneNode(true);
        ghost.className = 'gallery-img-fade-out';
        ghost.removeAttribute('onclick');
        wrap.appendChild(ghost);

        ghost.addEventListener('animationend', () => {
            ghost.remove();
        });

        oldImg.classList.remove('gallery-img-fade-in');
        void oldImg.offsetWidth;
        oldImg.classList.add('gallery-img-fade-in');
        oldImg.src = newUrl;
    });
    attachGalleryImageErrorHandlers();
}

function attachGalleryImageErrorHandlers() {
    if (!els.galleryGrid) return;
    els.galleryGrid.querySelectorAll('.gallery-card-img-wrap img').forEach(img => {
        if (img.dataset.errorHandlerAttached === '1') return;
        img.dataset.errorHandlerAttached = '1';
        img.addEventListener('load', () => markGalleryImageLoaded(img));
        img.addEventListener('error', () => markGalleryImageError(img));
    });
}

function markGalleryImageLoaded(img) {
    const wrap = img && img.closest('.gallery-card-img-wrap');
    if (!wrap) return;
    wrap.classList.remove('image-error');
    wrap.removeAttribute('data-error-label');
}

function markGalleryImageError(img) {
    const wrap = img && img.closest('.gallery-card-img-wrap');
    if (!wrap) return;
    wrap.classList.add('image-error');
    wrap.dataset.errorLabel = 'ИЗОБРАЖЕНИЕ НЕДОСТУПНО';
}

function getGalleryUrl(roleData) {
    if (galleryMode === 'raw') {
        return roleData.raw_overlay_url
            || roleData.raw_url
            || roleData.debug_url
            || '';
    }
    return roleData.debug_url
        || roleData.raw_overlay_url
        || roleData.raw_url
        || '';
}

function closeGallery() {
    if (els.galleryModal) {
        els.galleryModal.classList.add('is-hidden');
    }
    galleryData   = null;
    galleryPartId = null;
}

// ─── Global handlers ─────────────────────────────────────────

window._openPartGallery = function(partId) {
    openGallery(partId);
};

window._galleryImageLoaded = markGalleryImageLoaded;
window._galleryImageError = markGalleryImageError;

window._galleryFullscreen = function(src) {
    const existing = document.querySelector('.gallery-fullscreen');
    if (existing) existing.remove();

    const div = document.createElement('div');
    div.className = 'gallery-fullscreen';
    div.innerHTML = `<img src="${src}" alt="Полноэкранное изображение">`;
    div.addEventListener('click', () => div.remove());
    document.body.appendChild(div);
};

