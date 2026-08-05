// rule-summary.js — полностью переписанный блок анализа кадра
// Новый дизайн: как блок «ПОРОГИ ПРАВИЛ» — вкладки правил + карточка с порогами
// и тремя замерами под каждым порогом.
// Структура:
//   Геометрия входного окна
//     Низ зоны окон: макс. px [Значение]
//       [Значение] [Значение] [Значение]
//     Низ зоны окон: мин. px [Значение]
//       [Значение] [Значение] [Значение]
'use strict';

// ─── Helpers ────────────────────────────────────────────────
function faEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function faFormatValue(v) {
    if (v == null || v === '') return '—';
    if (typeof v === 'boolean') return v ? 'да' : 'нет';
    return String(v);
}

function faFormatLimit(metric) {
    if (!metric) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) {
        return String(metric.limit_raw);
    }
    return '—';
}

function faMeasurementClass(run) {
    if (!run) return 'is-neutral';
    if (run.ok === true) return 'is-ok';
    if (run.ok === false) return 'is-bad';
    return 'is-neutral';
}

// Δ = value - limit для подсказки близости к порогу
function faFormatDelta(run, limitRaw) {
    const valueRaw = run && typeof run.value_raw === 'number' ? run.value_raw : null;
    const limit = typeof limitRaw === 'number' ? limitRaw
        : (run && typeof run.limit_raw === 'number' ? run.limit_raw : null);
    if (valueRaw == null || limit == null) return null;
    if (!Number.isFinite(valueRaw) || !Number.isFinite(limit)) return null;
    const d = valueRaw - limit;
    if (Math.abs(d) < 1e-9) return '0';
    const abs = Math.abs(d);
    const body = abs >= 100 ? String(Math.round(abs))
        : abs >= 10 ? abs.toFixed(1)
        : abs.toFixed(2).replace(/\.?0+$/, '');
    return (d > 0 ? '+' : '−') + body;
}

// Человеческие названия правил — как в ТЗ оператора
const FA_RULE_LABELS = {
    window_geometry: 'Геометрия входного окна',
    window_sinks: 'Раковины в окнах',
    part_presence: 'Наличие корпуса',
    contacts_long: 'Длинные контакты',
    contacts_short: 'Короткие контакты',
    long_omission: 'Длинный пропуск',
    short_omission: 'Короткий пропуск',
    top_contacts: 'Контакты сверху',
    top_platform: 'Платформа',
    platform_contacts_overlap: 'Заплыв платформы',
    sinks: 'Раковины корпуса',
    glass: 'Стекло',
    glass_on_contacts: 'Стекло на контактах',
};

// Пороги для геометрии входного окна — запрос пользователя
const FA_THRESHOLD_LABELS = {
    // window_geometry
    top_px_min: 'Верх зоны окон: мин. px',
    top_px_max: 'Верх зоны окон: макс. px',
    bottom_px_min: 'Низ зоны окон: мин. px',
    bottom_px_max: 'Низ зоны окон: макс. px',
    top_limits: 'Верх зоны окон: допуск px',
    bottom_limits: 'Низ зоны окон: допуск px',
    // общие
    excess_component_min_px: 'Мин. размер фрагмента, px',
    top_line_max_residual_px: 'Отклонение верхней линии, px',
    line_tolerance_px: 'Допуск линии, px',
    omission_tilt_ratio_max: 'Макс. наклон, %',
    overlap_min_px: 'Мин. перекрытие, px',
};

// Ключи решающих порогов (из-за которых правило сработало)
function faDecisiveKeys(rule) {
    const keys = new Set();
    const breaches = Array.isArray(rule.threshold_breaches) ? rule.threshold_breaches : [];
    for (const b of breaches) {
        if (!b) continue;
        if (b.label) keys.add('label:' + b.label);
        if (b.key) keys.add('key:' + b.key);
        if (b.role && b.label) keys.add('role:' + b.role + '|label:' + b.label);
    }
    return keys;
}

function faIsDecisive(metric, role, decisiveKeys) {
    if (!decisiveKeys || !decisiveKeys.size) return false;
    if (metric.key && decisiveKeys.has('key:' + metric.key)) return true;
    if (metric.label && decisiveKeys.has('label:' + metric.label)) return true;
    if (role && metric.label && decisiveKeys.has('role:' + role + '|label:' + metric.label)) return true;
    return false;
}

// Собрать метрики по ролям из трёх прогонов: Map(role -> Map(key -> {label, limit, runs[3]}))
function faCollectMetrics(runCards) {
    const byRole = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards, runIndex) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            if (!card || typeof card !== 'object') continue;
            const role = card.role || '';
            if (!byRole.has(role)) byRole.set(role, new Map());
            const metrics = byRole.get(role);
            const mList = Array.isArray(card.metrics) ? card.metrics : [];
            for (const metric of mList) {
                if (!metric) continue;
                const key = metric.key || metric.label;
                if (!key) continue;
                if (!metrics.has(key)) {
                    metrics.set(key, {
                        label: metric.label || metric.key || '—',
                        key: metric.key || null,
                        limit: metric.limit || null,
                        limit_raw: metric.limit_raw,
                        runs: runs.map(() => null),
                    });
                }
                const entry = metrics.get(key);
                if (metric.limit != null && metric.limit !== '') entry.limit = metric.limit;
                if (metric.limit_raw !== undefined) entry.limit_raw = metric.limit_raw;
                if (metric.label) entry.label = metric.label;
                entry.runs[runIndex] = {
                    value: metric.value != null ? metric.value : null,
                    ok: metric.ok == null ? null : !!metric.ok,
                    value_raw: typeof metric.value_raw === 'number' ? metric.value_raw : null,
                    limit_raw: typeof metric.limit_raw === 'number' ? metric.limit_raw : null,
                };
            }
        }
    });
    return byRole;
}

// Одна строка порога: название + [порог] + три замера [знач] [знач] [знач]
function faBuildThresholdBlock(roleLabel, metric, pictureRun, isDecisive) {
    const block = faEl('div', 'fa-thr-item' + (isDecisive ? ' is-decisive' : ''));
    if (isDecisive) block.title = 'Решающий порог — из-за него сработало правило';

    // Заголовок строки: label | limit-box
    const head = faEl('div', 'fa-thr-head');
    const name = faEl('span', 'fa-thr-label');
    // Человеческое имя порога если есть в словаре
    const dictKey = metric.key || '';
    const niceLabel = FA_THRESHOLD_LABELS[dictKey] || metric.label || metric.key || '—';
    const fullLabel = roleLabel ? (roleLabel + ' · ' + niceLabel) : niceLabel;
    name.textContent = fullLabel;
    name.title = (metric.key || niceLabel) + (roleLabel ? ' (' + roleLabel + ')' : '');
    head.appendChild(name);

    const limit = faEl('span', 'fa-thr-limit');
    const limitText = faFormatLimit(metric);
    limit.textContent = limitText;
    limit.title = 'Порог: ' + limitText;
    limit.setAttribute('role', 'textbox');
    limit.setAttribute('aria-readonly', 'true');
    head.appendChild(limit);
    block.appendChild(head);

    // Три замера — как в примере пользователя
    const valuesRow = faEl('div', 'fa-thr-values');
    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    const limRaw = typeof metric.limit_raw === 'number' ? metric.limit_raw : null;

    for (let i = 0; i < 3; i++) {
        const run = runs[i] || null;
        const chip = faEl('span', 'fa-thr-value ' + faMeasurementClass(run));
        chip.dataset.run = String(i + 1);
        chip.setAttribute('role', 'button');
        chip.tabIndex = 0;
        chip.title = 'Прогон ' + (i + 1) + ' — показать кадр';

        const runLabel = faEl('span', 'fa-mv-run', 'П' + (i + 1));
        chip.appendChild(runLabel);
        const valueSpan = faEl('span', 'fa-mv-value', faFormatValue(run ? run.value : null));
        chip.appendChild(valueSpan);

        const deltaText = faFormatDelta(run, limRaw);
        if (deltaText != null) {
            const deltaSpan = faEl('span', 'fa-mv-delta', deltaText);
            if (run && run.ok === false) deltaSpan.classList.add('is-bad');
            else if (run && run.ok === true) deltaSpan.classList.add('is-ok');
            chip.appendChild(deltaSpan);
        }

        if (pictureRun && (i + 1) === pictureRun) {
            chip.classList.add('is-picture-run');
        }
        valuesRow.appendChild(chip);
    }
    block.appendChild(valuesRow);
    return block;
}

// Карточка одного правила — как карточка порогов правил
function faBuildRuleCard(rule, pictureRun, isActive) {
    const card = faEl('section', 'fa-card' + (isActive ? ' is-active' : ''));
    card.dataset.rule = rule.name || '';

    // Шапка карточки: название правила + статус
    const head = faEl('div', 'fa-card-head');
    const titleWrap = faEl('div', 'fa-card-title-wrap');
    const name = FA_RULE_LABELS[rule.name] || rule.name || 'Без названия';
    const title = faEl('span', 'fa-card-title', name);
    titleWrap.appendChild(title);
    // человеческая причина дефекта если есть
    if (rule.human_cause && rule.triggered) {
        const cause = faEl('span', 'fa-card-cause', rule.human_cause);
        titleWrap.appendChild(cause);
    }
    head.appendChild(titleWrap);

    const status = faEl('b', 'fa-card-status');
    const statusText = rule.status_label || (rule.skipped ? 'НЕ ВЫПОЛНЕНО' : (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА'));
    status.textContent = statusText;
    head.appendChild(status);
    card.appendChild(head);

    // Статус прогонов (область не построена и т.п.)
    const decisiveKeys = rule.triggered ? faDecisiveKeys(rule) : new Set();
    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    const runStatus = Array.isArray(rule.run_status) ? rule.run_status : [];

    // Строка статуса прогонов если есть проблемы
    if (runStatus.length) {
        const hasBad = runStatus.some(rows => (rows || []).some(r => (r.status || '').includes('НЕ') || (r.status || '').includes('ОБЛАСТЬ')));
        if (hasBad) {
            const statusStrip = faEl('div', 'fa-run-status');
            runStatus.forEach((rows, idx) => {
                if (!rows || !rows.length) return;
                const badInRun = rows.some(r => (r.status || '').includes('НЕ') || (r.status || '').includes('ОБЛАСТЬ') || r.status === 'ОТКЛОНЕНИЕ');
                if (!badInRun) return;
                rows.forEach(row => {
                    const chip = faEl('span', 'fa-run-chip' + ((row.status || '').includes('В НОРМЕ') ? ' is-ok' : ' is-bad'));
                    const roleTxt = row.role ? cameraRoleLabel(row.role) + ' · ' : '';
                    chip.textContent = 'П' + (idx + 1) + ': ' + roleTxt + (row.status || '—') + (row.reason ? ' (' + row.reason + ')' : '');
                    statusStrip.appendChild(chip);
                });
            });
            if (statusStrip.children.length) card.appendChild(statusStrip);
        }
    }

    // Тело карточки — скроллируемые пороги
    const rowsWrap = faEl('div', 'fa-rows');
    // Собираем метрики из трёх прогонов
    const byRole = faCollectMetrics(runCards);
    const blocks = [];
    let hasMetrics = false;

    if (byRole.size) {
        for (const [role, metricsMap] of byRole) {
            const roleLabel = role ? cameraRoleLabel(role) : '';
            for (const metric of metricsMap.values()) {
                const decisive = faIsDecisive(metric, role, decisiveKeys);
                blocks.push({
                    decisive,
                    node: faBuildThresholdBlock(roleLabel, metric, pictureRun, decisive),
                });
                hasMetrics = true;
            }
        }
        // решающие — сверху
        blocks.sort((a, b) => Number(b.decisive) - Number(a.decisive));
        blocks.forEach(b => rowsWrap.appendChild(b.node));
    }

    // Fallback: если нет run_cards, берём summary_cards текущего кадра
    if (!hasMetrics) {
        const summaryCards = Array.isArray(rule.summary_cards) ? rule.summary_cards : [];
        for (const cardData of summaryCards) {
            const role = cardData.role || '';
            const roleLabel = role ? cameraRoleLabel(role) : '';
            const mList = Array.isArray(cardData.metrics) ? cardData.metrics : [];
            for (const metric of mList) {
                const packed = {
                    label: metric.label || metric.key || '—',
                    key: metric.key || null,
                    limit: metric.limit || null,
                    limit_raw: metric.limit_raw,
                    runs: [{
                        value: metric.value != null ? metric.value : null,
                        ok: metric.ok == null ? null : metric.ok,
                        value_raw: typeof metric.value_raw === 'number' ? metric.value_raw : null,
                        limit_raw: typeof metric.limit_raw === 'number' ? metric.limit_raw : null,
                    }, null, null],
                };
                const decisive = faIsDecisive(packed, role, decisiveKeys);
                rowsWrap.appendChild(faBuildThresholdBlock(roleLabel, packed, pictureRun || 1, decisive));
                hasMetrics = true;
            }
        }
    }

    if (!hasMetrics) {
        // Нет метрик — показываем краткие строки (причины, детали)
        const lines = Array.isArray(rule.summary_lines) && rule.summary_lines.length
            ? rule.summary_lines
            : (Array.isArray(rule.detail_lines) ? rule.detail_lines : []);
        if (lines.length) {
            for (const line of lines) {
                const row = faEl('div', 'fa-detail-row', String(line));
                rowsWrap.appendChild(row);
            }
        } else if (rule.detail) {
            const row = faEl('div', 'fa-detail-row', String(rule.detail));
            rowsWrap.appendChild(row);
        } else {
            const empty = faEl('div', 'fa-empty', 'Нет измерений');
            rowsWrap.appendChild(empty);
        }
    }

    card.appendChild(rowsWrap);

    // Состояние карточки для рамки как у порогов — цвет по результату
    if (rule.part_absent) {
        card.classList.add('part-absent');
    } else if (rule.skipped) {
        card.classList.add('skipped');
    } else if (rule.triggered) {
        card.classList.add('triggered');
    } else {
        card.classList.add('ok');
    }

    return card;
}

// Основной рендер панели анализа кадра — табы + карточки
let faActiveIndex = 0;
let faPrevRender = null; // кэш предыдущего рендера для защиты от мигания
let faDrag = null; // состояние перетаскивания ползунка скролла (глобально для faSyncScroll)

function faRenderChanged(all, pictureRun) {
    if (!faPrevRender) return true;
    if (faPrevRender.length !== all.length) return true;
    for (let i = 0; i < all.length; i++) {
        if (faPrevRender[i].name !== all[i].name) return true;
        if (faPrevRender[i].triggered !== all[i].triggered) return true;
        if (faPrevRender[i].skipped !== all[i].skipped) return true;
        if (faPrevRender[i].status_label !== all[i].status_label) return true;
    }
    if (faPrevRender.pictureRun !== pictureRun) return true;
    return false;
}

function renderFrameAnalysisPanel(rules, pictureRun) {
    // Совместимость: старые id тоже обновляем если есть
    const tabsEl = document.getElementById('frame-analysis-tabs') || (typeof els !== 'undefined' && els.frameAnalysisTabs) || null;
    const cardsEl = document.getElementById('frame-analysis-cards') || (typeof els !== 'undefined' && els.frameAnalysisCards) || null;
    const titleEl = document.getElementById('frame-analysis-rules-title') || (typeof els !== 'undefined' && els.frameAnalysisRulesTitle) || null;

    if (!tabsEl || !cardsEl) {
        // fallback в старый контейнер если новый не найден
        const legacy = document.getElementById('frame-analysis-rules');
        if (legacy) {
            legacy.innerHTML = '';
            (rules || []).forEach(rule => {
                const g = document.createElement('div');
                g.textContent = (FA_RULE_LABELS[rule.name] || rule.name) + ' — ' + (rule.status_label || '');
                legacy.appendChild(g);
            });
        }
        return;
    }

    const all = Array.isArray(rules) ? rules : [];
    // Заголовок
    if (titleEl) {
        const showing = all.length;
        titleEl.textContent = showing ? ('ПРАВИЛА · ' + showing) : 'ПРАВИЛА';
    }

    if (!all.length) {
        tabsEl.innerHTML = '';
        cardsEl.innerHTML = '';
        const empty = faEl('div', 'fa-empty', 'Ожидание результатов правил');
        cardsEl.appendChild(empty);
        faPrevRender = null;
        return;
    }

    // Защита индекса
    if (faActiveIndex < 0 || faActiveIndex >= all.length) faActiveIndex = 0;

    // Защита от мигания: перерисовываем только если данные изменились
    const prev = faPrevRender;
    const changed = !prev || faRenderChanged(all, pictureRun);
    if (!changed) {
        // Индекс вкладки мог измениться без изменения данных — обновим активную вкладку
        const activeTab = tabsEl.querySelector('.fa-tab.is-active');
        if (activeTab && Number(activeTab.dataset.index) !== faActiveIndex) {
            tabsEl.querySelectorAll('.fa-tab').forEach(t => {
                const idx = Number(t.dataset.index);
                const isActive = idx === faActiveIndex;
                t.classList.toggle('is-active', isActive);
                t.setAttribute('aria-selected', isActive ? 'true' : 'false');
            });
            cardsEl.innerHTML = '';
            const activeRule = all[faActiveIndex];
            if (activeRule) {
                const card = faBuildRuleCard(activeRule, pictureRun, true);
                cardsEl.appendChild(card);
            }
            if (typeof faSyncScroll === 'function') requestAnimationFrame(() => faSyncScroll());
        }
        return;
    }

    faPrevRender = all.map(r => ({
        name: r.name,
        triggered: r.triggered,
        skipped: r.skipped,
        status_label: r.status_label,
    }));
    faPrevRender.pictureRun = pictureRun;

    // Рендер вкладок
    tabsEl.innerHTML = '';
    all.forEach((rule, idx) => {
        const tab = faEl('button', 'fa-tab' + (idx === faActiveIndex ? ' is-active' : ''));
        tab.type = 'button';
        tab.dataset.index = String(idx);
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', idx === faActiveIndex ? 'true' : 'false');
        const label = FA_RULE_LABELS[rule.name] || rule.name || '—';
        const span = faEl('span', 'fa-tab-label', label);
        tab.appendChild(span);
        if (rule.triggered) tab.classList.add('is-bad');
        else if (rule.skipped) tab.classList.add('is-warn');
        tab.title = label;
        tab.addEventListener('click', () => {
            faActiveIndex = idx;
            renderFrameAnalysisPanel(all, pictureRun);
            // скролл активной вкладки в зону видимости
            tab.scrollIntoView({block: 'nearest', inline: 'nearest', behavior: 'smooth'});
        });
        tabsEl.appendChild(tab);
    });

    // Рендер активной карточки
    cardsEl.innerHTML = '';
    const activeRule = all[faActiveIndex];
    if (activeRule) {
        const card = faBuildRuleCard(activeRule, pictureRun, true);
        cardsEl.appendChild(card);
    }

    // Синхронизация скролла как у порогов
    if (typeof faSyncScroll === 'function') {
        requestAnimationFrame(() => faSyncScroll());
    }
}

// Старые имена для совместимости с diagnostics.js
function renderRuleMeasurements(rule, pictureRun) {
    // Новый стиль уже содержит всё в карточке, но для совместимости вернём контейнер с порогами
    const wrap = faEl('div', 'fa-measurements');
    const byRole = faCollectMetrics(Array.isArray(rule.run_cards) ? rule.run_cards : []);
    const decisiveKeys = rule.triggered ? faDecisiveKeys(rule) : new Set();
    let added = false;
    for (const [role, map] of byRole) {
        const roleLabel = role ? cameraRoleLabel(role) : '';
        for (const metric of map.values()) {
            const decisive = faIsDecisive(metric, role, decisiveKeys);
            wrap.appendChild(faBuildThresholdBlock(roleLabel, metric, pictureRun, decisive));
            added = true;
        }
    }
    if (!added) {
        const sc = Array.isArray(rule.summary_cards) ? rule.summary_cards : [];
        sc.forEach(c => {
            (c.metrics || []).forEach(m => {
                const packed = {
                    label: m.label || m.key || '—',
                    key: m.key || null,
                    limit: m.limit || null,
                    limit_raw: m.limit_raw,
                    runs: [{value: m.value, ok: m.ok, value_raw: m.value_raw, limit_raw: m.limit_raw}, null, null],
                };
                wrap.appendChild(faBuildThresholdBlock(cameraRoleLabel(c.role || ''), packed, pictureRun, false));
            });
        });
    }
    return wrap;
}

function buildFrameAnalysisRuleGroup(rule, pictureRun) {
    // Для нового UI не используется, но оставим заглушку совместимости: строим карточку
    return faBuildRuleCard(rule, pictureRun, true);
}

function renderFrameAnalysisRules(rules, pictureRun) {
    // Новая панель — через табы
    renderFrameAnalysisPanel(rules, pictureRun);
}

// Скролл карточки анализа — как у порогов правил
function faSyncScroll() {
    const body = document.getElementById('frame-analysis-body');
    if (!body) return;
    const card = body.querySelector('.fa-card.is-active');
    if (!card) return;
    const rows = card.querySelector('.fa-rows');
    const track = document.getElementById('frame-analysis-scroll-track') || body.querySelector('.fa-scroll-track');
    const thumb = track ? track.querySelector('.fa-scroll-thumb') : null;
    if (!rows || !track || !thumb) return;
    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        thumb.style.height = '';
        rows.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    if (faDrag) return; // не перебивать позицию во время перетаскивания
    const trackH = track.clientHeight || 56;
    const ratio = rows.clientHeight / Math.max(1, rows.scrollHeight);
    const thumbH = Math.max(22, Math.min(Math.round(trackH * 0.6), Math.round(trackH * ratio)));
    const maxThumbTop = Math.max(0, trackH - thumbH);
    const top = maxScroll > 0 ? (rows.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbH + 'px';
    thumb.style.top = top + 'px';
}

function faSyncCardScroll(rows, track, thumb) {
    if (!rows || !track || !thumb) return;
    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        rows.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    // Не перебивать позицию thumb при скролле из wheel — scroll-обработчик уже обновил
    if (faDrag) return;
    const trackH = track.clientHeight || 56;
    const ratio = rows.clientHeight / Math.max(1, rows.scrollHeight);
    const thumbH = Math.max(22, Math.min(Math.round(trackH * 0.6), Math.round(trackH * ratio)));
    const maxThumbTop = Math.max(0, trackH - thumbH);
    const top = maxScroll > 0 ? (rows.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbH + 'px';
    thumb.style.top = top + 'px';
}

// Инициализация скролла после загрузки
(function setupFaScroll() {
    const init = () => {
        const body = document.getElementById('frame-analysis-body');
        if (!body) return;
        const track = document.getElementById('frame-analysis-scroll-track');
        if (!track) return;
        const thumb = track.querySelector('.fa-scroll-thumb');
        if (!thumb) return;
        const clamp = (v, mn, mx) => Math.max(mn, Math.min(mx, v));

        function onMove(e) {
            if (!faDrag) return;
            const {rows, track, thumb, startY, startTop, maxScroll, maxThumbTop} = faDrag;
            const dy = e.clientY - startY;
            const newTop = clamp(startTop + dy, 0, maxThumbTop);
            thumb.style.top = newTop + 'px';
            if (maxThumbTop > 0 && maxScroll > 0) {
                rows.scrollTop = (newTop / maxThumbTop) * maxScroll;
            }
        }
        function onUp() {
            if (!faDrag) return;
            const {thumb} = faDrag;
            faDrag.track.classList.remove('is-dragging');
            faDrag = null;
            if (thumb) thumb.style.transition = '';
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        }

        const card = body.querySelector('.fa-card.is-active');
        const rows = card ? card.querySelector('.fa-rows') : null;
        if (rows) {
            rows.addEventListener('scroll', () => {
                const activeRows = body.querySelector('.fa-card.is-active .fa-rows');
                const t = document.getElementById('frame-analysis-scroll-track');
                const th = t ? t.querySelector('.fa-scroll-thumb') : null;
                if (activeRows && t && th && !faDrag) faSyncCardScroll(activeRows, t, th);
            });
        }

        track.addEventListener('mousedown', (e) => {
            if (e.target === thumb) return;
            if (track.classList.contains('is-idle')) return;
            const activeRows = body.querySelector('.fa-card.is-active .fa-rows');
            if (!activeRows) return;
            const trackRect = track.getBoundingClientRect();
            const maxScroll = Math.max(0, activeRows.scrollHeight - activeRows.clientHeight);
            if (maxScroll <= 0) return;
            const trackH = track.clientHeight;
            const thumbH = thumb.offsetHeight || 22;
            const maxThumbTop = Math.max(0, trackH - thumbH);
            const clickY = e.clientY - trackRect.top;
            const desired = clamp(clickY - thumbH / 2, 0, maxThumbTop);
            thumb.style.transition = 'none';
            thumb.style.top = desired + 'px';
            activeRows.scrollTop = maxThumbTop > 0 ? (desired / maxThumbTop) * maxScroll : 0;
            requestAnimationFrame(() => { thumb.style.transition = ''; });
        });

        thumb.addEventListener('mousedown', (e) => {
            if (track.classList.contains('is-idle')) return;
            e.preventDefault();
            e.stopPropagation();
            const activeRows = body.querySelector('.fa-card.is-active .fa-rows');
            if (!activeRows) return;
            const trackH = track.clientHeight;
            const thumbH = thumb.offsetHeight || 22;
            const maxScroll = Math.max(0, activeRows.scrollHeight - activeRows.clientHeight);
            const maxThumbTop = Math.max(0, trackH - thumbH);
            const currentTop = parseFloat(thumb.style.top) || 0;
            faDrag = {rows: activeRows, track, thumb, startY: e.clientY, startTop: currentTop, maxScroll, maxThumbTop};
            track.classList.add('is-dragging');
            thumb.style.transition = 'none';
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });

        track.addEventListener('wheel', (e) => {
            if (track.classList.contains('is-idle')) return;
            if (faDrag) return;
            const activeRows = body.querySelector('.fa-card.is-active .fa-rows');
            if (!activeRows) return;
            e.preventDefault();
            activeRows.scrollTop += e.deltaY;
        }, {passive: false});
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        setTimeout(init, 100);
    }
    window.addEventListener('resize', () => faSyncScroll());
})();

// Экспорт в глобальную область для diagnostics.js и core
if (typeof window !== 'undefined') {
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
    window.renderFrameAnalysisRules = renderFrameAnalysisRules;
    window.renderRuleMeasurements = renderRuleMeasurements;
    window.buildFrameAnalysisRuleGroup = buildFrameAnalysisRuleGroup;
    window.faSyncScroll = faSyncScroll;
    window.FA_RULE_LABELS = FA_RULE_LABELS;
}
