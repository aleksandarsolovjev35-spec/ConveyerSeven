// thresholds.js — Line Monitor UI module
'use strict';

// Панель «Пороги правил» показывает параметры правил выбранной (главной)
// камеры и позволяет редактировать их. Редактирование доступно только до
// пуска (IDLE) и после полной остановки (STOPPED); во время работы линии
// панель скрыта, а backend дополнительно проверяет состояние при
// сохранении.

const THRESHOLD_EDITABLE_STATES = ['IDLE', 'STOPPED'];

let thresholdsData = null;       // последний ответ GET /api/thresholds
let thresholdsBusy = false;      // идёт загрузка порогов
let thresholdsSaveBusy = false;  // идёт сохранение порогов
let thresholdsBodyKey = null;    // роль+ревизия, для которой построены поля
let thresholdsDirty = false;     // оператор менял значения и ещё не сохранил
// Несохранённые правки названий: parameter -> новое название или null
// (null = убрать название, вернуть автоподпись).
let thresholdsLabelEdits = {};

function thresholdsPanelVisible() {
    return (
        !state.splashActive
        && !state.offline
        && !state.serverExitRequested
        && THRESHOLD_EDITABLE_STATES.includes(state.lineState)
        && !!state.currentCamera
    );
}

function thresholdsEditableNow() {
    return (
        thresholdsPanelVisible()
        && !state.controlPending
        && !state.startPending
        && !state.jogActive
        && !state.distributorDiagnosticPending
        && !state.distributorDiagnosticBackendBusy
        && !state.selectedAnalysisPending
    );
}

function setThresholdsStatus(message, kind) {
    if (!els.thresholdsStatus) return;
    setIfChanged(els.thresholdsStatus, message || '');
    els.thresholdsStatus.classList.toggle('is-error', kind === 'error');
}

async function fetchThresholds(role) {
    if (!role || thresholdsBusy) return;
    thresholdsBusy = true;
    const data = await apiGet(`/api/thresholds?role=${encodeURIComponent(role)}`);
    thresholdsBusy = false;
    if (!data) return;
    thresholdsData = data;
    renderThresholdsPanel();
}

// Вызывается из updateLineStatus (status.js), selectCamera/fetchCameras
// (cameras.js) и при офлайне: панель следует за выбранной главной камерой.
// force=true — принудительно перечитать пороги с сервера (автоподхват
// после внешней правки thresholds.json).
function updateThresholdsPanel(force) {
    if (!thresholdsPanelVisible()) {
        if (els.thresholdsPanel) els.thresholdsPanel.classList.add('is-hidden');
        return;
    }
    if (els.thresholdsPanel) els.thresholdsPanel.classList.remove('is-hidden');

    if (!thresholdsData || thresholdsData.role !== state.currentCamera) {
        // Смена камеры отбрасывает незаконченный ввод.
        thresholdsDirty = false;
        thresholdsLabelEdits = {};
        thresholdsData = null;
        thresholdsBodyKey = null;
        setThresholdsStatus('', '');
        fetchThresholds(state.currentCamera);
        return;
    }
    if (force && !thresholdsDirty && !thresholdsBusy) {
        fetchThresholds(state.currentCamera);
        return;
    }
    renderThresholdsPanel();
}

function renderThresholdsPanel() {
    if (!thresholdsData) return;

    if (els.thresholdsCameraLabel) {
        setIfChanged(els.thresholdsCameraLabel, cameraRoleLabel(state.currentCamera));
    }

    const editable = thresholdsEditableNow() && thresholdsData.editable !== false;
    if (els.thresholdsPanel) {
        els.thresholdsPanel.classList.toggle('is-locked', !editable);
    }
    if (els.thresholdsHint) {
        setIfChanged(els.thresholdsHint, editable
            ? 'Линия остановлена — значения можно менять'
            : 'Редактирование доступно до пуска и после полной остановки');
    }

    // Поля перестраиваются только при смене данных (роль/ревизия), а не на
    // каждом тике статуса — иначе незаконченный ввод оператора стирался бы
    // каждые 500 мс.
    const key = `${thresholdsData.role}|${thresholdsData.revision}`;
    if (key !== thresholdsBodyKey) {
        thresholdsBodyKey = key;
        renderThresholdsBody();
    }
    updateThresholdsActions();
}

function renderThresholdsBody() {
    const body = els.thresholdsBody;
    if (!body) return;
    body.innerHTML = '';

    const rules = (thresholdsData && thresholdsData.rules) || [];
    if (!rules.length) {
        const empty = document.createElement('div');
        empty.className = 'thresholds-empty';
        empty.textContent = 'Пороги для выбранной камеры не найдены';
        body.appendChild(empty);
        return;
    }

    const fragment = document.createDocumentFragment();
    for (const group of rules) {
        const groupEl = document.createElement('div');
        groupEl.className = 'thresholds-group';

        const title = document.createElement('div');
        title.className = 'thresholds-group-title';
        title.textContent = group.label || group.rule;
        groupEl.appendChild(title);

        const items = document.createElement('div');
        items.className = 'thresholds-items';

        for (const param of group.params || []) {
            const item = document.createElement('label');
            item.className = 'thresholds-item';

            const span = document.createElement('span');
            span.className = 'thresholds-item-label';
            span.textContent = effectiveThresholdLabel(param);
            span.title = param.key;

            const renameBtn = document.createElement('button');
            renameBtn.type = 'button';
            renameBtn.className = 'thresholds-rename-btn';
            renameBtn.textContent = '✎';
            renameBtn.title = 'Задать понятное название для оператора';
            renameBtn.addEventListener('click', (event) => {
                event.preventDefault();
                beginThresholdRename(item, param, renameBtn);
            });

            const input = document.createElement('input');
            input.type = 'number';
            input.className = 'thresholds-input';
            input.dataset.key = param.key;
            input.step = param.step || 'any';
            if (typeof param.min === 'number') input.min = param.min;
            if (typeof param.max === 'number') input.max = param.max;
            input.value = param.value;

            item.appendChild(span);
            item.appendChild(renameBtn);
            item.appendChild(input);
            items.appendChild(item);
        }

        groupEl.appendChild(items);
        fragment.appendChild(groupEl);
    }
    body.appendChild(fragment);
}

function effectiveThresholdLabel(param) {
    const edit = thresholdsLabelEdits[param.key];
    if (edit !== undefined) return edit || param.autoLabel || param.label || param.key;
    const custom = thresholdsData && thresholdsData.labels && thresholdsData.labels[param.key];
    return custom || param.autoLabel || param.label || param.key;
}

// Переименование порога: подпись заменяется полем ввода, Enter — принять,
// Esc — отменить, щелчок по кнопке «✓» — принять.
function beginThresholdRename(item, param, renameBtn) {
    const existing = item.querySelector('.thresholds-label-input');
    if (existing) {
        existing.focus();
        return;
    }
    const span = item.querySelector('.thresholds-item-label');
    if (!span) return;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'thresholds-label-input';
    input.value = effectiveThresholdLabel(param);
    input.maxLength = 80;

    let finished = false;
    const finish = (commit) => {
        if (finished) return;
        finished = true;
        if (commit) {
            const name = String(input.value).trim();
            if (!name || name === (param.autoLabel || param.key)) {
                // Пусто или совпадает с автоподписью — убираем своё название.
                thresholdsLabelEdits[param.key] = null;
            } else {
                thresholdsLabelEdits[param.key] = name;
            }
            thresholdsDirty = true;
        }
        span.textContent = effectiveThresholdLabel(param);
        span.style.display = '';
        input.remove();
        renameBtn.textContent = '✎';
        renameBtn.classList.remove('is-active');
        renameBtn.title = 'Задать понятное название для оператора';
        updateThresholdsActions();
    };

    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            finish(true);
        } else if (event.key === 'Escape') {
            event.preventDefault();
            finish(false);
        }
    });
    input.addEventListener('blur', () => finish(true));

    span.style.display = 'none';
    span.after(input);
    renameBtn.textContent = '✓';
    renameBtn.classList.add('is-active');
    renameBtn.title = 'Подтвердить название';
    input.focus();
    input.select();
}

function collectThresholdValues() {
    const values = {};
    if (els.thresholdsBody) {
        els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
            const raw = String(input.value).trim();
            if (raw === '') return;
            const number = Number(raw);
            if (Number.isNaN(number)) return;
            values[input.dataset.key] = number;
        });
    }
    return values;
}

// Итоговый набор названий роли для отправки: по каждому порогу явно указано
// название (пустая строка = убрать название, вернуть автоподпись).
function collectThresholdLabels() {
    const labels = {};
    if (els.thresholdsBody) {
        const server = (thresholdsData && thresholdsData.labels) || {};
        els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
            const key = input.dataset.key;
            if (thresholdsLabelEdits[key] !== undefined) {
                labels[key] = thresholdsLabelEdits[key] || '';
            } else {
                labels[key] = server[key] || '';
            }
        });
    }
    return labels;
}

function hasThresholdLabelChanges() {
    if (!thresholdsData || !els.thresholdsBody) return false;
    const server = thresholdsData.labels || {};
    let changed = false;
    els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
        const key = input.dataset.key;
        const requested = thresholdsLabelEdits[key] !== undefined
            ? (thresholdsLabelEdits[key] || '')
            : (server[key] || '');
        if ((server[key] || '') !== requested) changed = true;
    });
    return changed;
}

function hasChangedThresholds() {
    if (!thresholdsData || !thresholdsData.values) return false;
    const current = collectThresholdValues();
    const valuesChanged = Object.entries(current).some(
        ([key, value]) => thresholdsData.values[key] !== value,
    );
    return valuesChanged || hasThresholdLabelChanges();
}

function updateThresholdsActions() {
    if (!els.thresholdsSave || !els.thresholdsReset) return;
    const editable = thresholdsEditableNow() && !!thresholdsData;
    els.thresholdsReset.disabled = !editable || thresholdsSaveBusy;
    els.thresholdsSave.disabled = (
        !editable
        || thresholdsSaveBusy
        || !hasChangedThresholds()
    );
}

async function saveThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy || !thresholdsData) return;
    const values = collectThresholdValues();
    if (!Object.keys(values).length) return;

    // Пустые поля не сохраняются: честно предупреждаем, а не молча
    // пропускаем параметр.
    if (els.thresholdsBody) {
        const empty = els.thresholdsBody.querySelectorAll(
            'input.thresholds-input'
        );
        const hasEmpty = [...empty].some(
            input => String(input.value).trim() === '',
        );
        if (hasEmpty) {
            setThresholdsStatus('Заполните все поля', 'error');
            return;
        }
    }

    thresholdsSaveBusy = true;
    updateThresholdsActions();
    setThresholdsStatus('Сохранение...', '');
    try {
        const labels = collectThresholdLabels();
        const result = await apiPostJson('/api/thresholds', {
            role: state.currentCamera,
            values,
            labels,
        }, true);
        if (!result || !result.thresholds) {
            setThresholdsStatus('Не удалось сохранить пороги', 'error');
            return;
        }
        thresholdsData = result.thresholds;
        thresholdsDirty = false;
        thresholdsLabelEdits = {};
        if (typeof result.thresholds.revision === 'number') {
            state.thresholdsRevision = result.thresholds.revision;
        }
        setThresholdsStatus('Сохранено', '');
        renderThresholdsPanel();
    } finally {
        thresholdsSaveBusy = false;
        updateThresholdsActions();
    }
}

async function resetThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy) return;
    setThresholdsStatus('', '');
    // Принудительно перестраиваем поля, даже если ревизия не изменилась:
    // нужно сбросить незаконченный ввод оператора.
    thresholdsDirty = false;
    thresholdsLabelEdits = {};
    thresholdsBodyKey = null;
    thresholdsData = null;
    await fetchThresholds(state.currentCamera);
}

function setupThresholdsControls() {
    if (els.thresholdsSave) {
        els.thresholdsSave.addEventListener('click', saveThresholds);
    }
    if (els.thresholdsReset) {
        els.thresholdsReset.addEventListener('click', resetThresholds);
    }
    if (els.thresholdsBody) {
        els.thresholdsBody.addEventListener('input', () => {
            thresholdsDirty = true;
            updateThresholdsActions();
        });
    }
    updateThresholdsPanel();
}
