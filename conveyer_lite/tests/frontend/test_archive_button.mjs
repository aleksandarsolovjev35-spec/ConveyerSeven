// test_archive_button.mjs — видимость группы «АРХИВ» по состоянию линии.
//
// Кнопка и подпись настройки хранения партий — сервисное действие: группа
// видна только когда линия остановлена (IDLE/STOPPED), а при работе
// (RUNNING), паузе (PAUSED), остановке (STOPPING) и аварии (FAULT)
// скрывается целиком, включая подпись. Кнопка дополнительно блокируется
// при скрытии, а состояние OFFLINE тоже прячет группу.
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const button = __els['archive-settings-open'];
    const group = __els['archive-settings-group'];
    assert(button, 'archive button exists');
    assert(group, 'archive settings group exists');

    state.splashActive = false;
    state.offline = false;

    const setLineState = (s) => { state.lineState = s; updateArchiveButton(); };
    const groupHidden = () => group.classList._set.has('is-hidden');

    // Видна только на остановленной линии.
    setLineState('IDLE');
    assert(!groupHidden(), 'visible at IDLE');
    setLineState('STOPPED');
    assert(!groupHidden(), 'visible at STOPPED');

    // Скрыта при работе, паузе, остановке линии и аварии.
    setLineState('RUNNING');
    assert(groupHidden(), 'hidden at RUNNING');
    assert(button.disabled, 'disabled at RUNNING');
    setLineState('PAUSED');
    assert(groupHidden(), 'hidden at PAUSED');
    setLineState('STOPPING');
    assert(groupHidden(), 'hidden at STOPPING');
    setLineState('FAULT');
    assert(groupHidden(), 'hidden at FAULT');

    // Офлайн прячет группу, даже если состояние линии IDLE.
    setLineState('IDLE');
    assert(!groupHidden(), 'visible at IDLE before offline');
    state.offline = true;
    updateArchiveButton();
    assert(groupHidden(), 'hidden at OFFLINE');
    state.offline = false;

    // Сплаш тоже прячет группу.
    setLineState('IDLE');
    state.splashActive = true;
    updateArchiveButton();
    assert(groupHidden(), 'hidden during splash');
    state.splashActive = false;

    // Возврат в IDLE снова показывает группу.
    updateArchiveButton();
    assert(!groupHidden(), 'visible again at IDLE');

    console.log('TEST ARCHIVE BUTTON OK');
`;

await runInSandbox(sandbox, body);
