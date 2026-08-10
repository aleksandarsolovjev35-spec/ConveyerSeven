// frame-analysis.js — переработанный анализ кадра
// - без лишних обводок и кликабельности
// - с кастомным ползунком как в порогах правил
// - защита от гонок: версионирование рендеров, стабильная прокрутка,
//   идемпотентные обновления DOM
'use strict';

const FA_RULE_NAMES = {
    part_presence: 'Наличие корпуса',
    window_geometry: 'Геометрия входа',
    window_sinks: 'Раковины в окнах',
    contacts_long: 'Длинные контакты',
    contacts_short: 'Короткие контакты',
    long_omission: 'Длинная полоса',
    short_omission: 'Короткая полоса',
    top_contacts: 'Контакты сверху',
    top_platform: 'Платформа',
    platform_contacts_overlap: 'Заплыв платформы',
    sinks: 'Раковины корпуса',
    glass: 'Стекло',
    glass_on_contacts: 'Стекло на контактах',
};

// ——— состояние ———
let _faLastKey = null;
let _faLastScrollContext = null;
let _faLastStatsKey = null;
let _faRenderSeq = 0;
let _faScrollBound = false;
let _faDragState = null;
let _faRafSync = 0;

// ——— утилиты ———
function faNewVoteSummary(vote) {
    if (!vote) return {className: 'ok', text: '—'};
    const total = Number(vote.total_runs) || 1;
    const single = total <= 1;
    const count = (v) => single ? '' : ' · ' + (v ?? 0) + '/' + total;
    if (vote.decision === 'empty') return {className: 'warn', text: 'ПУСТО' + count(vote.empty_votes ?? vote.triggered_votes)};
    if (vote.decision === 'present') return {className: 'ok', text: 'КОРПУС' + count(vote.present_votes ?? vote.normal_votes)};
    if (vote.decision === 'triggered') return {className: 'bad', text: 'СРАБОТАЛО' + count(vote.triggered_votes)};
    return {className: 'ok', text: 'НОРМА' + count(vote.normal_votes)};
}

function faNewFormatValue(v) {
    if (v == null || v === '') return '—';
    return String(v);
}

function faNewFormatLimit(metric) {
    if (!metric) return '—';
    if (metric.limit != null && metric.limit !== '') return String(metric.limit);
    if (typeof metric.limit_raw === 'number' && Number.isFinite(metric.limit_raw)) return String(metric.limit_raw);
    return '—';
}

function faNewCollectThresholds(runCards) {
    // Собираем пороги из карточек единственного прогона.
    const map = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards, runIndex) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            const metrics = Array.isArray(card.metrics) ? card.metrics : [];
            for (const m of metrics) {
                const key = m.key || m.label;
                if (!key) continue;
                if (!map.has(key)) {
                    map.set(key, {
                        label: m.label || m.key || '—',
                        key: m.key || null,
                        limit: m.limit || null,
                        limit_raw: m.limit_raw,
                        runs: runs.map(() => null),
                    });
                }
                const entry = map.get(key);
                if (m.limit != null && m.limit !== '') entry.limit = m.limit;
                if (m.limit_raw !== undefined) entry.limit_raw = m.limit_raw;
                if (m.label) entry.label = m.label;
                entry.runs[runIndex] = {
                    value: m.value != null ? m.value : null,
                    ok: m.ok == null ? null : !!m.ok,
                    value_raw: typeof m.value_raw === 'number' ? m.value_raw : null,
                };
            }
        }
    });
    return map;
}

function faNewCollectGroups(runCards) {
    const generalMap = new Map();
    const objectsMap = new Map();
    const runs = Array.isArray(runCards) ? runCards : [];
    runs.forEach((cards) => {
        const list = Array.isArray(cards) ? cards : [];
        for (const card of list) {
            const role = card.role || '';
            const metrics = Array.isArray(card.metrics) ? card.metrics : [];
            for (const m of metrics) {
                const key = m.key || m.label;
                if (!key) continue;
                const row = {
                    label: m.label || m.key || '—',
                    limit: faNewFormatLimit(m),
                    value: faNewFormatValue(m.value != null ? m.value : null),
                    ok: m.ok == null ? null : !!m.ok,
                    value_raw: typeof m.value_raw === 'number' ? m.value_raw : null,
                };
                const objectName = m.object || null;
                if (!objectName) {
                    if (generalMap.has(key)) continue;
                    generalMap.set(key, row);
                } else {
                    const groupKey = role + '::' + objectName;
                    let group = objectsMap.get(groupKey);
                    if (!group) {
                        group = {name: objectName, rowsMap: new Map()};
                        objectsMap.set(groupKey, group);
                    }
                    if (group.rowsMap.has(key)) continue;
                    group.rowsMap.set(key, row);
                }
            }
        }
    });
    return {
        general: [...generalMap.values()],
        objects: [...objectsMap.values()].map(g => ({
            name: g.name,
            rows: [...g.rowsMap.values()],
        })),
    };
}

function faNewObjectStatus(rows) {
    let hasBad = false, hasOk = false, measured = false;
    for (const row of rows) {
        if (row.ok == null) continue;
        measured = true;
        if (row.ok) hasOk = true; else hasBad = true;
    }
    if (hasBad) return {cls: 'bad', text: 'ОТКЛОНЕНИЕ'};
    if (measured && hasOk) return {cls: 'ok', text: 'В НОРМЕ'};
    return {cls: 'muted', text: '—'};
}

function faNewStripObjectPrefix(label) {
    return String(label).replace(
        /^(?:Окно|Контакт|Раковина|Стекло|Shell|Glass)\s*#\d+(?:\s*\([^)]*\))?(?:\s*→\s*(?:контакт\s*)?#\d+)?\s*:\s*/i,
        '',
    );
}

function faNewReportKey(report) {
    try {
        return JSON.stringify({
            kind: report.kind,
            role: report.role,
            stage: report.stage,
            part_id: report.part_id,
            updated_at: report.updated_at,
            picture_run: report.picture_run,
            rules: report.rules,
        });
    } catch (_) {
        return [report.kind, report.role, report.stage, report.part_id, report.updated_at].join('|');
    }
}

function faNewVerdict(report, ls) {
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const absent = rules.find(r => r && r.part_absent === true);
    if (absent) return {cls: 'warn', text: 'КОРПУС НЕ ОБНАРУЖЕН'};
    const triggered = rules.filter(r => r && r.triggered === true);
    let category = null;
    if (ls && report.part_id != null) {
        const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
        const part = parts.find(p => Number(p.id) === Number(report.part_id));
        if (part) category = String(part.category || '').toUpperCase();
    }
    if (category === 'CLEANUP') return {cls: 'warn', text: 'ОЧИСТКА'};
    if (triggered.length) {
        const names = triggered.map(r => FA_RULE_NAMES[r.name] || r.name).join(', ');
        return {cls: 'bad', text: 'БРАК: ' + names};
    }
    if (category === 'BAD') return {cls: 'bad', text: 'БРАК'};
    if (rules.some(r => r && r.skipped === true)) return {cls: 'warn', text: 'ЕСТЬ ПРОПУЩЕННЫЕ ПРАВИЛА'};
    if (category === 'GOOD') return {cls: 'ok', text: 'ГОДНОЕ'};
    return {cls: 'ok', text: 'ГОДНО'};
}

function faNewUpdateStats(ls) {
    const totalEl = document.getElementById('fa-new-stat-total');
    if (!totalEl) return;
    const total = ls ? (Number(ls.total) || 0) : 0;
    const good = ls ? (Number(ls.good) || 0) : 0;
    const bad = ls ? (Number(ls.rejected) || 0) : 0;
    const cleanup = ls ? (Number(ls.cleanup) || 0) : 0;
    const key = [total, good, bad, cleanup].join('|');
    if (key === _faLastStatsKey) return;
    _faLastStatsKey = key;
    setIfChanged(document.getElementById('fa-new-stat-total'), total);
    setIfChanged(document.getElementById('fa-new-stat-good'), good);
    setIfChanged(document.getElementById('fa-new-stat-bad'), bad);
    setIfChanged(document.getElementById('fa-new-stat-cleanup'), cleanup);
}

// ——— ползунок ———
function faGetScrollEls() {
    // Fallback для тестового харнесса: если нового контейнера нет, используем старое тело
    const scroll = document.getElementById('fa-new-scroll') || document.getElementById('fa-new-body');
    return {
        scroll: scroll,
        track: document.getElementById('fa-new-scroll-track'),
        thumb: document.getElementById('fa-new-scroll-thumb'),
        body: document.getElementById('fa-new-body'),
        tbody: document.getElementById('fa-new-tbody'),
    };
}

function faSyncScroll() {
    const {scroll, track, thumb} = faGetScrollEls();
    if (!scroll || !track || !thumb) return;
    const maxScroll = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        if (scroll.scrollTop !== 0) scroll.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    const trackH = track.clientHeight || 80;
    const ratio = scroll.clientHeight / Math.max(1, scroll.scrollHeight);
    const thumbH = Math.max(22, Math.min(Math.round(trackH * 0.55), Math.round(trackH * ratio)));
    const maxThumbTop = Math.max(0, trackH - thumbH);
    const top = maxScroll > 0 ? (scroll.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbH + 'px';
    thumb.style.top = top + 'px';
}

function faOnScroll() {
    if (_faRafSync) cancelAnimationFrame(_faRafSync);
    _faRafSync = requestAnimationFrame(() => {
        _faRafSync = 0;
        faSyncScroll();
    });
}

function faClamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

function faInitScrollHandlers() {
    if (_faScrollBound) return;
    const {scroll, track, thumb} = faGetScrollEls();
    if (!scroll || !track || !thumb) return;
    _faScrollBound = true;

    scroll.addEventListener('scroll', faOnScroll, {passive: true});

    // Клик по дорожке — быстрый переход
    track.addEventListener('mousedown', (e) => {
        if (e.target === thumb) return;
        if (track.classList.contains('is-idle')) return;
        const rect = track.getBoundingClientRect();
        const maxScroll = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
        if (maxScroll <= 0) return;
        const trackH = track.clientHeight;
        const thumbH = thumb.offsetHeight || 22;
        const maxThumbTop = Math.max(0, trackH - thumbH);
        const clickY = e.clientY - rect.top;
        const desiredTop = faClamp(clickY - thumbH / 2, 0, maxThumbTop);
        thumb.style.top = desiredTop + 'px';
        scroll.scrollTop = maxThumbTop > 0 ? (desiredTop / maxThumbTop) * maxScroll : 0;
    });

    // Перетаскивание бегунка
    thumb.addEventListener('mousedown', (e) => {
        if (track.classList.contains('is-idle')) return;
        e.preventDefault();
        e.stopPropagation();
        const trackH = track.clientHeight;
        const thumbH = thumb.offsetHeight || 22;
        const maxScroll = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
        const maxThumbTop = Math.max(0, trackH - thumbH);
        const startTop = parseFloat(thumb.style.top) || 0;
        _faDragState = {scroll, track, thumb, startY: e.clientY, startTop, maxScroll, maxThumbTop};
        track.classList.add('is-dragging');
        const onMove = (ev) => {
            if (!_faDragState) return;
            const {scroll: sc, thumb: th, startY, startTop: st, maxScroll: ms, maxThumbTop: mt} = _faDragState;
            const dy = ev.clientY - startY;
            const nt = faClamp(st + dy, 0, mt);
            th.style.top = nt + 'px';
            if (mt > 0 && ms > 0) sc.scrollTop = (nt / mt) * ms;
        };
        const onUp = () => {
            if (!_faDragState) return;
            const {track: tr} = _faDragState;
            tr.classList.remove('is-dragging');
            _faDragState = null;
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });

    // Колесо на дорожке — прокрутка контента
    track.addEventListener('wheel', (e) => {
        if (track.classList.contains('is-idle')) return;
        e.preventDefault();
        scroll.scrollTop += e.deltaY;
    }, {passive: false});

    // Клавиатура — стрелками
    track.addEventListener('keydown', (e) => {
        if (track.classList.contains('is-idle')) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); scroll.scrollTop += 40; }
        if (e.key === 'ArrowUp') { e.preventDefault(); scroll.scrollTop -= 40; }
        if (e.key === 'PageDown') { e.preventDefault(); scroll.scrollTop += scroll.clientHeight * 0.8; }
        if (e.key === 'PageUp') { e.preventDefault(); scroll.scrollTop -= scroll.clientHeight * 0.8; }
        if (e.key === 'Home') { e.preventDefault(); scroll.scrollTop = 0; }
        if (e.key === 'End') { e.preventDefault(); scroll.scrollTop = scroll.scrollHeight; }
    });

    window.addEventListener('resize', () => faOnScroll());
}

// ——— построение DOM ———
function faNewBuildRow(row) {
    const rowEl = document.createElement('div');
    rowEl.className = 'fa-new-thr-row';

    const label = document.createElement('span');
    label.className = 'fa-new-thr-label';
    label.textContent = row.label;
    label.title = row.label;
    rowEl.appendChild(label);

    const limit = document.createElement('span');
    limit.className = 'fa-new-thr-limit';
    limit.textContent = row.limit;
    limit.title = row.limit;
    rowEl.appendChild(limit);

    const meas = document.createElement('span');
    meas.className = 'fa-new-meas' +
        (row.ok === false ? ' is-bad' : '') +
        (row.ok === true ? ' is-ok' : '');
    meas.textContent = row.value;
    meas.title = row.value;
    // не кликабельно: никакого cursor pointer, никаких обработчиков
    rowEl.appendChild(meas);
    return rowEl;
}

function renderNewFrameAnalysis(report, ls) {
    _faRenderSeq += 1;
    const thisSeq = _faRenderSeq;

    const panel = document.getElementById('frame-analysis-panel');
    // Fallback: в тестовом харнессе нет fa-new-scroll, используем fa-new-body как скролл-контейнер
    let scroll = document.getElementById('fa-new-scroll');
    if (!scroll) scroll = document.getElementById('fa-new-body');
    const tbody = document.getElementById('fa-new-tbody');
    if (!panel || !scroll || !tbody) return;

    faInitScrollHandlers();
    faNewUpdateStats(ls);

    const available = report.available === true;
    if (!available) {
        if (!panel.classList.contains('is-collapsed')) panel.classList.add('is-collapsed');
        // очищаем только если был контент
        if (tbody.children.length) tbody.replaceChildren();
        _faLastKey = null;
        _faLastScrollContext = null;
        requestAnimationFrame(() => faSyncScroll());
        return;
    }
    if (panel.classList.contains('is-collapsed')) panel.classList.remove('is-collapsed');

    const rules = Array.isArray(report.rules) ? report.rules : [];

    // вердикт + контекст — точечно
    const verdictEl = document.getElementById('fa-new-verdict');
    if (verdictEl) {
        const verdict = faNewVerdict(report, ls);
        const cls = 'fa-new-verdict ' + verdict.cls;
        if (verdictEl.className !== cls) verdictEl.className = cls;
        setIfChanged(verdictEl, verdict.text);
    }
    const contextEl = document.getElementById('fa-new-context');
    if (contextEl) {
        const parts = [];
        const stage = String(report.stage || '').toUpperCase();
        if (stage) parts.push(stage);
        if (report.role) parts.push(typeof cameraRoleLabel === 'function' ? cameraRoleLabel(report.role) : report.role);
        if (report.part_id != null) parts.push('КОРПУС #' + report.part_id);
        setIfChanged(contextEl, parts.join(' · '));
    }

    const key = faNewReportKey(report);
    if (key === _faLastKey && tbody.children.length) {
        // контент тот же — не трогаем DOM, но синхронизируем скролл на всякий случай
        requestAnimationFrame(() => faSyncScroll());
        return;
    }

    // Сохраняем прокрутку перед перестроением
    const keepScroll = scroll.scrollTop;
    const scrollContext = String(report.stage || '') + '|' + (report.part_id == null ? '' : report.part_id);
    const resetScroll = _faLastScrollContext !== null && _faLastScrollContext !== scrollContext;

    _faLastKey = key;

    if (!rules.length) {
        tbody.replaceChildren();
        const emptyRow = document.createElement('div');
        emptyRow.className = 'fa-new-empty';
        emptyRow.textContent = 'Ожидание результатов анализа…';
        tbody.appendChild(emptyRow);
        if (thisSeq === _faRenderSeq) scroll.scrollTop = 0;
        requestAnimationFrame(() => faSyncScroll());
        _faLastScrollContext = scrollContext;
        return;
    }

    // Собираем фрагмент вне DOM для стабильности; fallback для харнесса
    let frag = null;
    let fragList = null;
    if (typeof document.createDocumentFragment === 'function') {
        frag = document.createDocumentFragment();
    } else {
        fragList = [];
        frag = {
            appendChild: (el) => { fragList.push(el); return el; },
            _isFake: true,
            _list: fragList,
        };
    }
    const sorted = [...rules].sort((a, b) => {
        const order = r => r.part_absent ? 0 : (r.triggered ? 1 : (r.skipped ? 2 : 3));
        return order(a) - order(b);
    });

    for (const rule of sorted) {
        const ruleHead = document.createElement('div');
        ruleHead.className = 'fa-new-rule-head' + (rule.triggered || rule.part_absent ? ' triggered' : '');
        const ruleName = document.createElement('span');
        ruleName.className = 'fa-new-rule-name';
        ruleName.textContent = FA_RULE_NAMES[rule.name] || rule.name;
        ruleName.title = FA_RULE_NAMES[rule.name] || rule.name;
        ruleHead.appendChild(ruleName);

        const vote = faNewVoteSummary(rule.vote_details);
        const badge = document.createElement('span');
        badge.className = 'fa-new-rule-badge ' + vote.className;
        badge.textContent = vote.text;
        ruleHead.appendChild(badge);
        frag.appendChild(ruleHead);

        if (rule.part_absent) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = 'КОРПУС НЕ ОБНАРУЖЕН — измерения недоступны';
            frag.appendChild(emptyRow);
            continue;
        }

        const groups = faNewCollectGroups(rule.run_cards);
        if (!groups.general.length && !groups.objects.length) {
            const emptyRow = document.createElement('div');
            emptyRow.className = 'fa-new-empty';
            emptyRow.textContent = rule.skipped ? 'Нет измерений' : 'Нет данных порогов';
            frag.appendChild(emptyRow);
            continue;
        }

        for (const row of groups.general) {
            frag.appendChild(faNewBuildRow(row));
        }

        for (const object of groups.objects) {
            const block = document.createElement('div');
            block.className = 'fa-new-obj-block';

            const head = document.createElement('div');
            head.className = 'fa-new-obj-head';
            const name = document.createElement('span');
            name.className = 'fa-new-obj-name';
            name.textContent = object.name;
            name.title = object.name;
            head.appendChild(name);

            const status = faNewObjectStatus(object.rows);
            const statusBadge = document.createElement('span');
            statusBadge.className = 'fa-new-obj-badge ' + status.cls;
            statusBadge.textContent = status.text;
            head.appendChild(statusBadge);
            block.appendChild(head);

            for (const row of object.rows) {
                const rowEl = faNewBuildRow(row);
                const labelEl = rowEl.children[0];
                if (labelEl) labelEl.textContent = faNewStripObjectPrefix(row.label);
                block.appendChild(rowEl);
            }
            frag.appendChild(block);
        }
    }

    // Проверяем, что рендер всё ещё актуален (защита от гонки быстрых статусов)
    if (thisSeq !== _faRenderSeq) return;

    if (frag && frag._isFake) {
        tbody.replaceChildren();
        frag._list.forEach(el => { try { tbody.appendChild(el); } catch (_) { tbody.children.push(el); } });
    } else {
        tbody.replaceChildren(frag);
    }

    // Восстанавливаем прокрутку
    if (resetScroll) scroll.scrollTop = 0;
    else scroll.scrollTop = keepScroll;

    _faLastScrollContext = scrollContext;
    requestAnimationFrame(() => faSyncScroll());
}

function updateNewFrameAnalysisStatus(ls) {
    const report = ls.frame_analysis || {};
    renderNewFrameAnalysis(report, ls);
}

function renderFrameAnalysisPanel() { return null; }

if (typeof window !== 'undefined') {
    window.renderNewFrameAnalysis = renderNewFrameAnalysis;
    window.updateNewFrameAnalysisStatus = updateNewFrameAnalysisStatus;
    window.FA_RULE_NAMES = FA_RULE_NAMES;
    window.renderFrameAnalysisPanel = renderFrameAnalysisPanel;
    window.faSyncScroll = faSyncScroll;
}
