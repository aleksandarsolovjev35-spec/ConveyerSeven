// rule-summary.js — блок анализа кадра (вкладки + карточка с порогами и тремя замерами)
// Legacy: fa-threshold, buildThresholdBlock, fa-threshold-runs, formatDeltaSimple — см. также .fa-thr-* классы
'use strict';

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
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) return String(metric.limit_raw);
    return '—';
}

function faMeasurementClass(run) {
    if (!run) return 'is-neutral';
    if (run.ok === true) return 'is-ok';
    if (run.ok === false) return 'is-bad';
    return 'is-neutral';
}

function faFormatDelta(run, limitRaw) {
    const valueRaw = run && typeof run.value_raw === 'number' ? run.value_raw : null;
    const limit = typeof limitRaw === 'number' ? limitRaw : (run && typeof run.limit_raw === 'number' ? run.limit_raw : null);
    if (valueRaw == null || limit == null) return null;
    if (!Number.isFinite(valueRaw) || !Number.isFinite(limit)) return null;
    const d = valueRaw - limit;
    if (Math.abs(d) < 1e-9) return '0';
    const abs = Math.abs(d);
    const body = abs >= 100 ? String(Math.round(abs)) : abs >= 10 ? abs.toFixed(1) : abs.toFixed(2).replace(/\.?0+$/, '');
    return (d > 0 ? '+' : '−') + body;
}

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

const FA_THRESHOLD_LABELS = {
    top_px_min: 'Верх зоны окон: мин. px',
    top_px_max: 'Верх зоны окон: макс. px',
    bottom_px_min: 'Низ зоны окон: мин. px',
    bottom_px_max: 'Низ зоны окон: макс. px',
    top_limits: 'Верх зоны окон: допуск px',
    bottom_limits: 'Низ зоны окон: допуск px',
    excess_component_min_px: 'Мин. размер фрагмента, px',
    top_line_max_residual_px: 'Отклонение верхней линии, px',
    line_tolerance_px: 'Допуск линии, px',
    omission_tilt_ratio_max: 'Макс. наклон, %',
    overlap_min_px: 'Мин. перекрытие, px',
};

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
                    metrics.set(key, { label: metric.label || metric.key || '—', key: metric.key || null, limit: metric.limit || null, limit_raw: metric.limit_raw, runs: runs.map(() => null) });
                }
                const entry = metrics.get(key);
                if (metric.limit != null && metric.limit !== '') entry.limit = metric.limit;
                if (metric.limit_raw !== undefined) entry.limit_raw = metric.limit_raw;
                if (metric.label) entry.label = metric.label;
                entry.runs[runIndex] = { value: metric.value != null ? metric.value : null, ok: metric.ok == null ? null : !!metric.ok, value_raw: typeof metric.value_raw === 'number' ? metric.value_raw : null, limit_raw: typeof metric.limit_raw === 'number' ? metric.limit_raw : null };
            }
        }
    });
    return byRole;
}

function faBuildThresholdBlock(roleLabel, metric, pictureRun, isDecisive) {
    const block = faEl('div', 'fa-thr-item fa-threshold' + (isDecisive ? ' is-decisive' : ''));
    if (isDecisive) block.title = 'Решающий порог — из-за него сработало правило';
    const head = faEl('div', 'fa-thr-head');
    const name = faEl('span', 'fa-thr-label fa-threshold-name');
    const dictKey = metric.key || '';
    const niceLabel = FA_THRESHOLD_LABELS[dictKey] || metric.label || metric.key || '—';
    const fullLabel = roleLabel ? (roleLabel + ' · ' + niceLabel) : niceLabel;
    name.textContent = fullLabel;
    name.title = (metric.key || niceLabel) + (roleLabel ? ' (' + roleLabel + ')' : '');
    head.appendChild(name);
    const limit = faEl('span', 'fa-thr-limit fa-threshold-limit');
    limit.textContent = faFormatLimit(metric);
    limit.title = 'Порог: ' + limit.textContent;
    limit.setAttribute('role', 'textbox');
    limit.setAttribute('aria-readonly', 'true');
    head.appendChild(limit);
    block.appendChild(head);
    const valuesRow = faEl('div', 'fa-thr-values fa-threshold-runs');
    const runs = Array.isArray(metric.runs) ? metric.runs : [];
    const limRaw = typeof metric.limit_raw === 'number' ? metric.limit_raw : null;
    for (let i = 0; i < 3; i++) {
        const run = runs[i] || null;
        const chip = faEl('span', 'fa-thr-value fa-measurement-value ' + faMeasurementClass(run));
        chip.setAttribute('data-run', String(i + 1));
        chip.setAttribute('role', 'button');
        chip.tabIndex = 0;
        chip.title = 'Прогон ' + (i + 1) + ' — показать кадр';
        chip.appendChild(faEl('span', 'fa-mv-run', 'П' + (i + 1)));
        chip.appendChild(faEl('span', 'fa-mv-value', faFormatValue(run ? run.value : null)));
        const deltaText = faFormatDelta(run, limRaw);
        if (deltaText != null) {
            const deltaSpan = faEl('span', 'fa-mv-delta', deltaText);
            if (run && run.ok === false) deltaSpan.classList.add('is-bad');
            else if (run && run.ok === true) deltaSpan.classList.add('is-ok');
            chip.appendChild(deltaSpan);
        }
        if (pictureRun && (i + 1) === pictureRun) chip.classList.add('is-picture-run');
        valuesRow.appendChild(chip);
    }
    block.appendChild(valuesRow);
    return block;
}

function faGetObjIndex(label) {
    if (!label) return null;
    let m = label.match(/(?:Окно|Контакт|Раковина|Стекло|Shell)\s*(?:корпуса\s*)?#(\d+)/i);
    if (m) return Number(m[1]);
    m = label.match(/#(\d+)/);
    if (m) return Number(m[1]);
    return null;
}

function faGroupAndAppendBlocks(rule, blocks, rowsWrap, pictureRun) {
    if (!blocks || !blocks.length) return;

    if (rule && rule.part_absent) {
        const banner = faEl('div', 'fa-empty-status', 'КОРПУС НЕ ОБНАРУЖЕН');
        rowsWrap.appendChild(banner);
        return;
    }

    const getOrder = (b) => {
        const lbl = b.metric && (b.metric.label || b.metric.key || '');
        const idx = faGetObjIndex(lbl);
        if (idx != null) return 1000 + idx;
        if (lbl.includes('Группа ') || lbl.includes('группа ')) return 2000;
        if (lbl.includes('Допуск') || lbl.includes('Наклон') || lbl.includes('эталона') || lbl.includes('Ширина') || lbl.includes('Высота')) return 3000;
        return 0;
    };

    const sorted = [...blocks].sort((a, b) => {
        const oA = getOrder(a);
        const oB = getOrder(b);
        if (oA !== oB) return oA - oB;
        return 0;
    });

    const objBlocks = new Map();
    const generalItems = [];
    const lineParamsItems = [];
    const groupStatsItems = [];
    const ruleName = rule ? rule.name : '';

    for (const b of sorted) {
        const lbl = b.metric && (b.metric.label || b.metric.key || '');
        const idx = faGetObjIndex(lbl);

        if (ruleName === 'top_contacts' && (lbl.includes('Группа ') || lbl.includes('группа '))) {
            groupStatsItems.push(b);
            continue;
        }

        if ((ruleName === 'contacts_long' || ruleName === 'contacts_short') &&
            (lbl.includes('Допуск') || lbl.includes('Наклон') || lbl.includes('эталона') || lbl.includes('Ширина') || lbl.includes('Высота'))) {
            lineParamsItems.push(b);
            continue;
        }

        if (idx != null) {
            let objType = 'Объект';
            if (/Окно/i.test(lbl) || /win_/i.test(b.metric?.key || '')) objType = 'Окно';
            else if (/Контакт/i.test(lbl) || /contact_/i.test(b.metric?.key || '')) objType = 'Контакт';
            else if (/Раковина|Shell/i.test(lbl) || /sink_/i.test(b.metric?.key || '')) objType = 'Раковина корпуса';
            else if (/Стекло|glass_/i.test(lbl)) objType = 'Стекло';

            let groupKey = objType + ' #' + idx;
            if (ruleName === 'window_sinks') {
                const wm = lbl.match(/окно\s*#(\d+)/i) || (b.metric?.key || '').match(/win_(\d+)/i);
                if (wm) groupKey = `Окно #${Number(wm[1])}`;
            } else if (ruleName === 'glass_on_contacts') {
                const pm = lbl.match(/Стекло\s*#(\d+)\s*→\s*контакт\s*#(\d+)/i);
                if (pm) groupKey = `Стекло #${Number(pm[1])} → контакт #${Number(pm[2])}`;
            } else if (ruleName === 'top_contacts') {
                const gm = lbl.match(/#(\d+)\s+([LRTB])/i);
                if (gm) groupKey = `Контакт #${idx} ${gm[2].toUpperCase()}`;
            }

            if (!objBlocks.has(groupKey)) {
                objBlocks.set(groupKey, {
                    title: groupKey,
                    status: 'Обнаружено',
                    items: []
                });
            }
            objBlocks.get(groupKey).items.push(b);
        } else {
            generalItems.push(b);
        }
    }

    for (const b of generalItems) {
        rowsWrap.appendChild(b.node);
    }

    for (const [key, group] of objBlocks) {
        let isBad = false;
        for (const item of group.items) {
            if (item.metric && item.metric.ok === false) isBad = true;
            if (item.metric && Array.isArray(item.metric.runs)) {
                for (const run of item.metric.runs) {
                    if (run && run.ok === false) isBad = true;
                }
            }
        }
        let statusText = 'В норме';
        let statusCls = 'fa-obj-status is-ok';
        if (/Раковина|Стекло|glass|sink/i.test(key)) {
            statusText = isBad ? 'Брак · Пересечение' : 'Обнаружено';
            statusCls = isBad ? 'fa-obj-status is-bad' : 'fa-obj-status is-ok';
        } else if (/Окно/i.test(key)) {
            statusText = isBad ? 'Вне допуска' : 'В допуске';
            statusCls = isBad ? 'fa-obj-status is-bad' : 'fa-obj-status is-ok';
        } else if (/Контакт/i.test(key)) {
            statusText = isBad ? 'Вне допуска' : 'В норме';
            statusCls = isBad ? 'fa-obj-status is-bad' : 'fa-obj-status is-ok';
        } else if (isBad) {
            statusText = 'Отклонение';
            statusCls = 'fa-obj-status is-bad';
        }

        const groupEl = faEl('div', 'fa-obj-block');
        const header = faEl('div', 'fa-obj-header');
        header.appendChild(faEl('span', 'fa-obj-title', group.title));
        header.appendChild(faEl('span', statusCls, statusText));
        groupEl.appendChild(header);
        const content = faEl('div', 'fa-obj-content');
        for (const item of group.items) {
            content.appendChild(item.node);
        }
        groupEl.appendChild(content);
        rowsWrap.appendChild(groupEl);
    }

    if (lineParamsItems.length) {
        const lineEl = faEl('div', 'fa-obj-block fa-line-params-block');
        const header = faEl('div', 'fa-obj-header');
        header.appendChild(faEl('span', 'fa-obj-title', 'Параметры линии и эталона'));
        lineEl.appendChild(header);
        const content = faEl('div', 'fa-obj-content');
        for (const item of lineParamsItems) {
            content.appendChild(item.node);
        }
        lineEl.appendChild(content);
        rowsWrap.appendChild(lineEl);
    }

    if (groupStatsItems.length) {
        const grpEl = faEl('div', 'fa-obj-block fa-group-stats-block');
        const header = faEl('div', 'fa-obj-header');
        header.appendChild(faEl('span', 'fa-obj-title', 'Групповые статистики (L/R/T/B)'));
        grpEl.appendChild(header);
        const content = faEl('div', 'fa-obj-content');
        for (const item of groupStatsItems) {
            content.appendChild(item.node);
        }
        grpEl.appendChild(content);
        rowsWrap.appendChild(grpEl);
    }
}

function faBuildRuleCard(rule, pictureRun, isActive) {
    const card = faEl('section', 'fa-card frame-analysis-item' + (isActive ? ' is-active' : ''));
    card.dataset.rule = rule.name || '';
    const head = faEl('div', 'fa-card-head');
    const titleWrap = faEl('div', 'fa-card-title-wrap');
    const name = FA_RULE_LABELS[rule.name] || rule.name || 'Без названия';
    titleWrap.appendChild(faEl('span', 'fa-card-title', name));
    if (rule.human_cause && rule.triggered) titleWrap.appendChild(faEl('span', 'fa-card-cause', rule.human_cause));
    head.appendChild(titleWrap);
    const status = faEl('b', 'fa-card-status');
    status.textContent = rule.status_label || (rule.skipped ? 'НЕ ВЫПОЛНЕНО' : (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА'));
    head.appendChild(status);
    card.appendChild(head);
    const decisiveKeys = rule.triggered ? faDecisiveKeys(rule) : new Set();
    const runCards = Array.isArray(rule.run_cards) ? rule.run_cards : [];
    const runStatus = Array.isArray(rule.run_status) ? rule.run_status : [];
    if (runStatus.length) {
        const hasBad = runStatus.some(rows => (rows || []).some(r => (r.status || '').includes('НЕ') || (r.status || '').includes('ОБЛАСТЬ')));
        if (hasBad) {
            const statusStrip = faEl('div', 'fa-run-status');
            runStatus.forEach((rows, idx) => {
                const list = (Array.isArray(rows) && rows.length) ? rows : [{role: '', status: '—', reason: null}];
                list.forEach(row => {
                    const ok = (row.status || '').includes('В НОРМЕ');
                    const cls = 'fa-run-chip fa-run-status-chip' + (ok ? ' is-ok' : ' is-bad') + (((idx + 1) === (pictureRun || 1)) ? ' is-picture-run' : '');
                    const chip = faEl('span', cls);
                    chip.textContent = 'П' + (idx + 1) + ': ' + (row.role ? cameraRoleLabel(row.role) + ' · ' : '') + (row.status || '—') + (row.reason ? ' (' + row.reason + ')' : '');
                    statusStrip.appendChild(chip);
                });
            });
            if (statusStrip.children.length) card.appendChild(statusStrip);
        }
    }
    const rowsWrap = faEl('div', 'fa-rows fa-measurements');
    const byRole = faCollectMetrics(runCards);
    const blocks = [];
    let hasMetrics = false;
    if (byRole.size) {
        for (const [role, metricsMap] of byRole) {
            const roleLabel = role ? cameraRoleLabel(role) : '';
            for (const metric of metricsMap.values()) {
                const decisive = faIsDecisive(metric, role, decisiveKeys);
                blocks.push({ decisive, node: faBuildThresholdBlock(roleLabel, metric, pictureRun, decisive), metric, roleLabel });
                hasMetrics = true;
            }
        }
        faGroupAndAppendBlocks(rule, blocks, rowsWrap, pictureRun);
    }
    if (!hasMetrics) {
        const summaryCards = Array.isArray(rule.summary_cards) ? rule.summary_cards : [];
        for (const cardData of summaryCards) {
            const role = cardData.role || '';
            const roleLabel = role ? cameraRoleLabel(role) : '';
            const mList = Array.isArray(cardData.metrics) ? cardData.metrics : [];
            for (const metric of mList) {
                const packed = { label: metric.label || metric.key || '—', key: metric.key || null, limit: metric.limit || null, limit_raw: metric.limit_raw, runs: [{ value: metric.value != null ? metric.value : null, ok: metric.ok == null ? null : metric.ok, value_raw: typeof metric.value_raw === 'number' ? metric.value_raw : null, limit_raw: typeof metric.limit_raw === 'number' ? metric.limit_raw : null }, null, null] };
                rowsWrap.appendChild(faBuildThresholdBlock(roleLabel, packed, pictureRun || 1, faIsDecisive(packed, role, decisiveKeys)));
                hasMetrics = true;
            }
        }
    }
    if (!hasMetrics) {
        const lines = Array.isArray(rule.summary_lines) && rule.summary_lines.length ? rule.summary_lines : (Array.isArray(rule.detail_lines) ? rule.detail_lines : []);
        if (lines.length) {
            for (const line of lines) rowsWrap.appendChild(faEl('div', 'fa-detail-row', String(line)));
        } else if (rule.detail) {
            rowsWrap.appendChild(faEl('div', 'fa-detail-row', String(rule.detail)));
        } else {
            rowsWrap.appendChild(faEl('div', 'fa-empty', 'Нет измерений'));
        }
    }
    card.appendChild(rowsWrap);
    if (rule.part_absent) card.classList.add('part-absent');
    else if (rule.skipped) card.classList.add('skipped');
    else if (rule.triggered) card.classList.add('triggered');
    else card.classList.add('ok');
    return card;
}

let faActiveIndex = 0;
let faPrevRender = null;
let faDrag = null;

function faRuleRenderSignature(rule) {
    // Статус правила может оставаться тем же, пока меняются сами замеры.
    // Сигнатура только по name/triggered оставляла на экране значения
    // предыдущего анализа. Данные приходят из JSON, поэтому полная
    // сериализация безопасна и заодно учитывает run_cards, run_status,
    // пороги и fallback summary_cards.
    try {
        return JSON.stringify(rule || null);
    } catch (error) {
        // Защита от неожиданного не-сериализуемого поля: карточка всё равно
        // должна быть перерисована, а не показывать устаревшую статистику.
        return String(rule);
    }
}

function faRenderChanged(all, pictureRun) {
    if (!faPrevRender) return true;
    if (faPrevRender.length !== all.length) return true;
    for (let i = 0; i < all.length; i++) {
        if (faPrevRender[i].signature !== faRuleRenderSignature(all[i])) {
            return true;
        }
    }
    if (faPrevRender.pictureRun !== pictureRun) return true;
    return false;
}

function renderFrameAnalysisPanel(rules, pictureRun) {
    const tabsEl = document.getElementById('frame-analysis-tabs') || (typeof els !== 'undefined' && els.frameAnalysisTabs) || null;
    const cardsEl = document.getElementById('frame-analysis-cards') || (typeof els !== 'undefined' && els.frameAnalysisCards) || null;
    const titleEl = document.getElementById('frame-analysis-rules-title') || (typeof els !== 'undefined' && els.frameAnalysisRulesTitle) || null;
    if (!tabsEl || !cardsEl) {
        const legacy = document.getElementById('frame-analysis-rules');
        if (legacy) {
            legacy.innerHTML = '';
            (rules || []).forEach(rule => { const g = document.createElement('div'); g.textContent = (FA_RULE_LABELS[rule.name] || rule.name) + ' — ' + (rule.status_label || ''); legacy.appendChild(g); });
        }
        return;
    }
    const all = Array.isArray(rules) ? rules : [];
    if (titleEl) titleEl.textContent = all.length ? ('ПРАВИЛА · ' + all.length) : 'ПРАВИЛА';
    if (!all.length) {
        tabsEl.innerHTML = '';
        cardsEl.innerHTML = '';
        cardsEl.appendChild(faEl('div', 'fa-empty', 'Ожидание результатов правил'));
        const legacy = document.getElementById('frame-analysis-rules');
        if (legacy) legacy.innerHTML = '';
        faPrevRender = null;
        return;
    }
    if (faActiveIndex < 0 || faActiveIndex >= all.length) faActiveIndex = 0;
    const prev = faPrevRender;
    const changed = !prev || faRenderChanged(all, pictureRun);
    if (!changed) {
        const activeTab = tabsEl.querySelector('.fa-tab.is-active');
        if (activeTab && Number(activeTab.dataset.index) !== faActiveIndex) {
            tabsEl.querySelectorAll('.fa-tab').forEach(t => { const idx = Number(t.dataset.index); const isActive = idx === faActiveIndex; t.classList.toggle('is-active', isActive); t.setAttribute('aria-selected', isActive ? 'true' : 'false'); });
            cardsEl.innerHTML = '';
            const activeRule = all[faActiveIndex];
            if (activeRule) cardsEl.appendChild(faBuildRuleCard(activeRule, pictureRun, true));
            if (typeof faSyncScroll === 'function') requestAnimationFrame(() => faSyncScroll());
        }
        const legacy = document.getElementById('frame-analysis-rules');
        if (legacy && legacy.children.length !== all.length) {
            legacy.innerHTML = '';
            all.forEach(rule => {
                legacy.appendChild(faBuildRuleCard(rule, pictureRun, true));
            });
        }
        return;
    }
    faPrevRender = all.map(rule => ({
        name: rule.name,
        signature: faRuleRenderSignature(rule),
    }));
    faPrevRender.pictureRun = pictureRun;
    tabsEl.innerHTML = '';
    all.forEach((rule, idx) => {
        const tab = faEl('button', 'fa-tab' + (idx === faActiveIndex ? ' is-active' : ''));
        tab.type = 'button';
        tab.dataset.index = String(idx);
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', idx === faActiveIndex ? 'true' : 'false');
        const label = FA_RULE_LABELS[rule.name] || rule.name || '—';
        tab.appendChild(faEl('span', 'fa-tab-label', label));
        if (rule.triggered) tab.classList.add('is-bad');
        else if (rule.skipped) tab.classList.add('is-warn');
        tab.title = label;
        tab.addEventListener('click', () => { faActiveIndex = idx; renderFrameAnalysisPanel(all, pictureRun); tab.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' }); });
        tabsEl.appendChild(tab);
    });
    cardsEl.innerHTML = '';
    const activeRule = all[faActiveIndex];
    if (activeRule) cardsEl.appendChild(faBuildRuleCard(activeRule, pictureRun, true));
    const legacy = document.getElementById('frame-analysis-rules');
    if (legacy) {
        legacy.innerHTML = '';
        all.forEach(rule => {
            legacy.appendChild(faBuildRuleCard(rule, pictureRun, true));
        });
    }
    if (typeof faSyncScroll === 'function') requestAnimationFrame(() => faSyncScroll());
}

function renderRuleMeasurements(rule, pictureRun) {
    const wrap = faEl('div', 'fa-measurements');
    const byRole = faCollectMetrics(Array.isArray(rule.run_cards) ? rule.run_cards : []);
    const decisiveKeys = rule.triggered ? faDecisiveKeys(rule) : new Set();
    let added = false;
    for (const [role, map] of byRole) {
        const roleLabel = role ? cameraRoleLabel(role) : '';
        for (const metric of map.values()) { wrap.appendChild(faBuildThresholdBlock(roleLabel, metric, pictureRun, faIsDecisive(metric, role, decisiveKeys))); added = true; }
    }
    if (!added) {
        const sc = Array.isArray(rule.summary_cards) ? rule.summary_cards : [];
        sc.forEach(c => { (c.metrics || []).forEach(m => { const packed = { label: m.label || m.key || '—', key: m.key || null, limit: m.limit || null, limit_raw: m.limit_raw, runs: [{ value: m.value, ok: m.ok, value_raw: m.value_raw, limit_raw: m.limit_raw }, null, null] }; wrap.appendChild(faBuildThresholdBlock(cameraRoleLabel(c.role || ''), packed, pictureRun, false)); }); });
    }
    return wrap;
}

function buildFrameAnalysisRuleGroup(rule, pictureRun) { return faBuildRuleCard(rule, pictureRun, true); }

function renderFrameAnalysisRules(rules, pictureRun) { renderFrameAnalysisPanel(rules, pictureRun); }

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
    if (maxScroll <= 0) { track.classList.add('is-idle'); thumb.style.top = '0px'; thumb.style.height = ''; rows.scrollTop = 0; return; }
    track.classList.remove('is-idle');
    if (faDrag) return;
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
    if (maxScroll <= 0) { track.classList.add('is-idle'); thumb.style.top = '0px'; rows.scrollTop = 0; return; }
    track.classList.remove('is-idle');
    if (faDrag) return;
    const trackH = track.clientHeight || 56;
    const ratio = rows.clientHeight / Math.max(1, rows.scrollHeight);
    const thumbH = Math.max(22, Math.min(Math.round(trackH * 0.6), Math.round(trackH * ratio)));
    const maxThumbTop = Math.max(0, trackH - thumbH);
    const top = maxScroll > 0 ? (rows.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbH + 'px';
    thumb.style.top = top + 'px';
}

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
            const { rows, track, thumb, startY, startTop, maxScroll, maxThumbTop } = faDrag;
            const newTop = clamp(startTop + (e.clientY - startY), 0, maxThumbTop);
            thumb.style.top = newTop + 'px';
            if (maxThumbTop > 0 && maxScroll > 0) rows.scrollTop = (newTop / maxThumbTop) * maxScroll;
        }
        function onUp() {
            if (!faDrag) return;
            const { thumb } = faDrag;
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
            const desired = clamp(e.clientY - trackRect.top - thumbH / 2, 0, maxThumbTop);
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
            faDrag = { rows: activeRows, track, thumb, startY: e.clientY, startTop: currentTop, maxScroll, maxThumbTop };
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
        }, { passive: false });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else setTimeout(init, 100);
    window.addEventListener('resize', () => faSyncScroll());
})();

if (typeof window !== 'undefined') {
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
    window.renderFrameAnalysisRules = renderFrameAnalysisRules;
    window.renderRuleMeasurements = renderRuleMeasurements;
    window.buildFrameAnalysisRuleGroup = buildFrameAnalysisRuleGroup;
    window.faSyncScroll = faSyncScroll;
    window.FA_RULE_LABELS = FA_RULE_LABELS;
}
