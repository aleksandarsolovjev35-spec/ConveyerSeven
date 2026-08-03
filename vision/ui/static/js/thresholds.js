// thresholds.js — Line Monitor UI module
'use strict';

// Панель «Пороги правил» показывает параметры правил выбранной (главной)
// камеры с понятными русскими названиями (label приходит с сервера) и
// позволяет менять только значения. Редактирование доступно только до
// пуска (IDLE) и после полной остановки (STOPPED); во время работы линии
// панель скрыта, а backend дополнительно проверяет состояние при
// сохранении.

const THRESHOLD_EDITABLE_STATES = ['IDLE', 'STOPPED'];

let thresholdsData = null;       // последний ответ GET /api/thresholds
let thresholdsBusy = false;      // идёт загрузка порогов
let thresholdsSaveBusy = false;  // идёт сохранение порогов
let thresholdsBodyKey = null;    // роль+ревизия, для которой построены поля
let thresholdsDirty = false;     // оператор менял значения и ещё не сохранил
let thresholdsActiveRule = null; // открытая вкладка правила в редакторе

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
    // Гонка при быстром переключении камер: ответ прилетел после того, как
    // оператор уже выбрал другую камеру — данные старой камеры отбрасываем
    // и подгружаем актуальные.
    if (data.role !== state.currentCamera) {
        fetchThresholds(state.currentCamera);
        return;
    }
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
        thresholdsActiveRule = null;
        thresholdsData = null;
        thresholdsBodyKey = null;
        setThresholdsStatus('', '');
        fetchThresholds(state.currentCamera);
        return;
    }
    // Автоподхват после внешней правки файла: не перезапрашивать, пока
    // оператор не сохранил свои незаконченные изменения.
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

    // Вместо внутренней прокрутки каждая группа правила открывается на
    // собственной вкладке. Все поля остаются в DOM: правки на другой
    // вкладке не теряются и одним действием сохраняются целиком.
    const activeRules = rules.map((group, index) => group.rule || String(index));
    if (!activeRules.includes(thresholdsActiveRule)) {
        thresholdsActiveRule = activeRules[0];
    }

    const tabs = document.createElement('div');
    tabs.className = 'thresholds-tabs';
    tabs.setAttribute('role', 'tablist');
    const panels = document.createDocumentFragment();

    rules.forEach((group, index) => {
        const ruleId = group.rule || String(index);
        const isActive = ruleId === thresholdsActiveRule;
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'thresholds-tab';
        tab.dataset.rule = ruleId;
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', String(isActive));
        tab.textContent = group.label || group.rule;
        tabs.appendChild(tab);

        const groupEl = document.createElement('div');
        groupEl.className = 'thresholds-group thresholds-tab-panel';
        groupEl.dataset.rule = ruleId;
        groupEl.setAttribute('role', 'tabpanel');
        groupEl.hidden = !isActive;

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
            span.textContent = param.label || param.key;
            span.title = param.key;

            const input = document.createElement('input');
            input.type = 'number';
            input.className = 'thresholds-input';
            input.dataset.key = param.key;
            input.step = param.step || 'any';
            if (typeof param.min === 'number') input.min = param.min;
            if (typeof param.max === 'number') input.max = param.max;
            input.value = param.value;
            item.append(span, input);
            items.appendChild(item);
        }
        groupEl.appendChild(items);
        panels.appendChild(groupEl);
    });
    body.append(tabs, panels);
}

function selectThresholdRule(rule) {
    if (!rule || !els.thresholdsBody) return;
    thresholdsActiveRule = rule;
    els.thresholdsBody.querySelectorAll('.thresholds-tab').forEach(tab => {
        const active = tab.dataset.rule === rule;
        tab.classList.toggle('is-active', active);
        tab.setAttribute('aria-selected', String(active));
    });
    els.thresholdsBody.querySelectorAll('.thresholds-tab-panel').forEach(panel => {
        panel.hidden = panel.dataset.rule !== rule;
    });
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

function hasChangedThresholds() {
    if (!thresholdsData || !thresholdsData.values) return false;
    // Пустое поле — тоже незаконченное изменение: кнопка «СОХРАНИТЬ»
    // становится активной, а сохранение честно сообщает «Заполните все поля».
    if (els.thresholdsBody) {
        const hasEmpty = [...els.thresholdsBody.querySelectorAll(
            'input.thresholds-input'
        )].some(input => String(input.value).trim() === '');
        if (hasEmpty) return true;
    }
    const current = collectThresholdValues();
    return Object.entries(current).some(
        ([key, value]) => thresholdsData.values[key] !== value,
    );
}

function updateThresholdsActions() {
    if (!els.thresholdsSave || !els.thresholdsReset) return;
    // ``editable`` от API — отдельная защита: панель может быть видимой в
    // момент, когда backend уже запретил правку (например, начинается пуск).
    const editable = (
        thresholdsEditableNow()
        && !!thresholdsData
        && thresholdsData.editable !== false
    );
    // Не полагаемся только на сравнение чисел. Некоторые браузеры присылают
    // ``change`` для стрелок number-поля позже, чем оператор ожидает, а
    // ``input`` — при ручном вводе. Флаг гарантирует, что после любого
    // редактирования кнопка сохранения сразу доступна.
    const changed = thresholdsDirty || hasChangedThresholds();
    els.thresholdsReset.disabled = !editable || thresholdsSaveBusy;
    els.thresholdsSave.disabled = !editable || thresholdsSaveBusy || !changed;
}

async function saveThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy || !thresholdsData) return;

    // Пустые поля не сохраняются: честно предупреждаем, а не молча
    // пропускаем параметр (проверяем до сбора значений, иначе очищенное
    // поле дало бы пустой values и молчаливый выход).
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

    const values = collectThresholdValues();
    if (!Object.keys(values).length) return;

    thresholdsSaveBusy = true;
    updateThresholdsActions();
    setThresholdsStatus('Сохранение...', '');
    try {
        const result = await apiPostJson('/api/thresholds', {
            role: state.currentCamera,
            values,
        }, true);
        if (!result || !result.thresholds) {
            setThresholdsStatus('Не удалось сохранить пороги', 'error');
            return;
        }
        thresholdsData = result.thresholds;
        thresholdsDirty = false;
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
        els.thresholdsBody.addEventListener('click', event => {
            const tab = event.target.closest('button.thresholds-tab');
            if (tab) selectThresholdRule(tab.dataset.rule);
        });
        const markThresholdsChanged = event => {
            // Делегирование сохраняет обработчик и после перестроения полей.
            if (!event.target.matches('input.thresholds-input')) return;
            thresholdsDirty = true;
            // Если оператор вернул значения как было (0.3 -> 0.5 -> 0.3),
            // изменений больше нет — снимаем блокировку автоподхвата.
            if (!hasChangedThresholds()) thresholdsDirty = false;
            updateThresholdsActions();
        };
        // ``input`` покрывает набор с клавиатуры, ``change`` — стрелки и
        // автозаполнение number-поля в браузерах, где input приходит поздно.
        els.thresholdsBody.addEventListener('input', markThresholdsChanged);
        els.thresholdsBody.addEventListener('change', markThresholdsChanged);
    }
    updateThresholdsPanel();
}
