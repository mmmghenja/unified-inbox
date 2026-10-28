// Ф99-5: карта глобальных горячих клавиш (`docs/architecture.md` с поправкой П9). Здесь — привязка к
// существующим функциям стора и компонентов; сам механизм — `util/hotkeys.ts`. «Разрешить» — `Ctrl+Enter` главной
// кнопкой (уже есть), одиночных букв на карточках нет.
import { boardState, closeColumn, itemKey, toggleWait, type ColumnItem } from '../store/board';
import { store } from '../store/instance';
import { toggleTerminal } from '../store/terminal';
import { openWrite } from '../store/writeModal';
import { hotkeyBus, hotkeyHelp, inField, registerHotkeys, type Hotkey } from './hotkeys';
import { projectTabs } from './tabs';

const ROW = '.wait-bar-pop .home-react-row';

function visibleRows(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(ROW)];
}

/** `J`/`K`: фокус на следующую/предыдущую строку «Ждёт тебя» (Enter на кнопке-строке открывает её окном сам). */
export function moveRow(delta: 1 | -1): boolean {
  const rows = visibleRows();
  if (rows.length === 0) return false;
  const at = rows.findIndex((el) => el === document.activeElement);
  const next = at === -1 ? (delta === 1 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, at + delta));
  rows[next]?.focus();
  rows[next]?.scrollIntoView?.({ block: 'nearest' });
  return true;
}

function focusedItem(): { items: ColumnItem[]; at: number } {
  const items = boardState.layout.open;
  return { items, at: items.findIndex((o) => itemKey(o) === boardState.focusKey) };
}

/** `Alt+←/→`: подсветить соседний столбец доски (прокрутку делает `BoardColumns` по `focusSeq`). */
export function moveColumnFocus(delta: 1 | -1): boolean {
  if (!store.state.homeOpen) return false;
  const { items, at } = focusedItem();
  if (items.length === 0) return false;
  const next = at === -1 ? (delta === 1 ? 0 : items.length - 1) : Math.min(items.length - 1, Math.max(0, at + delta));
  boardState.focusKey = itemKey(items[next]!);
  boardState.focusSeq += 1;
  return true;
}

/** `Alt+X`: закрыть столбец в фокусе. */
export function closeFocusedColumn(): boolean {
  if (!store.state.homeOpen) return false;
  const { items, at } = focusedItem();
  if (at === -1) return false;
  closeColumn(items[at]!);
  boardState.focusKey = null;
  return true;
}

function openTab(n: number): boolean {
  const tab = projectTabs(store.state.projects)[n - 1];
  if (tab === undefined) return false;
  void store.openProject(tab.id);
  return true;
}

function toggleWaitPanel(): void {
  if (!store.state.homeOpen) store.openHome();
  toggleWait(boardState.waitShown ? null : 'all');
}

/** Поле ввода сессии: то, что в фокусе, иначе первое на странице. */
function focusSessionField(): boolean {
  const field = document.querySelector<HTMLElement>('.session-window .work-say-text');
  if (field === null) return false;
  field.focus();
  return true;
}

function stopFirstSession(): boolean {
  const btn = document.querySelector<HTMLButtonElement>('.session-window .session-stop');
  if (btn === null || btn.disabled) return false;
  btn.click();
  return true;
}

export function globalHotkeys(): Hotkey[] {
  const keys: Hotkey[] = [
    { code: 'Slash', shift: true, label: '?', where: 'везде', desc: 'эта шпаргалка', run: () => void (hotkeyHelp.open = !hotkeyHelp.open) },
    { code: 'Digit0', alt: true, label: 'Alt+0', where: 'везде', desc: 'Главная', run: () => store.openHome() },
  ];
  for (let n = 1; n <= 9; n += 1) {
    keys.push({
      code: `Digit${n}`,
      alt: true,
      label: `Alt+${n}`,
      where: 'везде',
      desc: n === 1 ? 'вкладка проекта 1…9' : `вкладка проекта ${n}`,
      run: () => openTab(n),
    });
  }
  keys.push(
    { code: 'KeyW', alt: true, label: 'Alt+W', where: 'везде', desc: '«Ждёт тебя» открыть/закрыть', run: toggleWaitPanel },
    { code: 'KeyN', alt: true, label: 'Alt+N', where: 'везде', desc: '«Написать…»', run: () => openWrite() },
    { code: 'KeyM', alt: true, label: 'Alt+M', where: 'везде', desc: '«Новое сообщение»', run: () => void (hotkeyBus.newMessage += 1) },
    { code: 'KeyT', alt: true, label: 'Alt+T', where: 'везде', desc: 'терминал показать/скрыть', run: () => toggleTerminal() },
    { code: 'Slash', label: '/', where: 'вне полей ввода', desc: 'поиск', run: () => void (hotkeyBus.search += 1) },
    { code: 'KeyJ', label: 'J', where: '«Ждёт тебя»', desc: 'следующая строка (Enter — открыть окном)', run: () => moveRow(1), when: () => visibleRows().length > 0 },
    { code: 'KeyK', label: 'K', where: '«Ждёт тебя»', desc: 'предыдущая строка', run: () => moveRow(-1), when: () => visibleRows().length > 0 },
    { code: 'ArrowLeft', alt: true, label: 'Alt+←', where: 'доска', desc: 'предыдущий столбец', run: () => moveColumnFocus(-1), when: () => store.state.homeOpen },
    { code: 'ArrowRight', alt: true, label: 'Alt+→', where: 'доска', desc: 'следующий столбец', run: () => moveColumnFocus(1), when: () => store.state.homeOpen },
    { code: 'KeyX', alt: true, label: 'Alt+X', where: 'доска', desc: 'закрыть столбец в фокусе', run: closeFocusedColumn },
    { code: 'KeyS', alt: true, label: 'Alt+S', where: 'окно сессии', desc: 'фокус в поле ввода', run: focusSessionField },
    { code: 'Period', alt: true, label: 'Alt+.', where: 'окно сессии', desc: '«Прервать» ход', run: stopFirstSession },
  );
  return keys;
}

/** Включить глобальную карту; вернуть функцию снятия (зовёт `MainApp`). */
export function installGlobalHotkeys(): () => void {
  return registerHotkeys({ keys: globalHotkeys() });
}

export { inField };
