// Ф48-5 (`docs/architecture.md`): Claude Code закрепляет инструкцию роли при
// создании сессии. Окно «Инструкция роли» при `pinned === false` показывает строку «Файл изменён после создания
// сессии…»; подписи поля «Файл роли» в формах новой сессии и «Роль…» говорят про закрепление.
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../src/api/client';
import type { HubSession, SessionRoleText } from '../src/api/types';
import NewSessionForm from '../src/components/NewSessionForm.vue';
import RoleTextModal from '../src/components/RoleTextModal.vue';
import SessionRoleForm from '../src/components/SessionRoleForm.vue';
import { resetBoard } from '../src/store/board';
import { resetRoles } from '../src/store/roles';

const UNPINNED =
  'Файл изменён после создания сессии: сессия получает новую редакцию сообщением при каждом запуске. Закрепить её в системном тексте — «Новая с передачей дел».';
const PIN_HINT =
  'Claude Code закрепляет инструкцию при создании сессии; правку файла сессия получит сообщением при следующем запуске, насовсем — в новой сессии. OpenCode читает файл при каждом запуске.';

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

function text(overrides: Partial<SessionRoleText> = {}): SessionRoleText {
  return {
    role: 'тестер 1',
    common: 'Общий свод',
    role_file: 'docs/ROLE.md',
    file_text: '# Роль',
    file_reason: null,
    ...overrides,
  };
}

let reply: SessionRoleText;

enableAutoUnmount(afterEach);

beforeEach(() => {
  api.setCsrf('c');
  api.setUnauthorizedHandler(null);
  resetBoard();
  resetRoles();
  reply = text();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string) => {
      const path = new URL(input, 'http://localhost').pathname;
      if (path.endsWith('/role-text')) return json(reply);
      return json([]);
    }),
  );
});

afterEach(() => {
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function open() {
  mount(RoleTextModal, { props: { sessionId: 5, title: 'Тестер' }, attachTo: document.body });
  await flushPromises();
  return document.body.querySelector('.role-text-file-unpinned');
}

describe('Ф48-5: окно «Инструкция роли» — файл изменён после создания сессии', () => {
  it('pinned === false — строка про новую редакцию', async () => {
    reply = text({ pinned: false });
    expect((await open())?.textContent?.trim()).toBe(UNPINNED);
  });

  it('файл снят после создания сессии — строка тоже есть', async () => {
    reply = text({ role_file: null, file_text: null, pinned: false });
    expect((await open())?.textContent?.trim()).toBe(UNPINNED);
    expect(document.body.querySelector('.role-text-file--none')).not.toBeNull();
  });

  it.each([
    ['совпадает', { pinned: true }],
    ['снимка нет или OpenCode', { pinned: null }],
    ['старое ядро без поля', {}],
  ])('%s — строки нет', async (_name, extra) => {
    reply = text(extra as Partial<SessionRoleText>);
    expect(await open()).toBeNull();
    expect(document.body.textContent).not.toContain('Файл изменён после создания сессии');
  });
});

describe('Ф48-5: подписи поля «Файл роли»', () => {
  it('форма новой сессии', () => {
    const w = mount(NewSessionForm, { props: { projectId: 3 } });
    expect(w.get('.session-role-file-hint').text()).toContain(PIN_HINT);
    expect(w.text()).not.toContain('со следующего запуска процесса');
  });

  it('форма «Роль…»', () => {
    const session = { id: 5, project_id: 3, role: 'тестер', role_no: 1, engine: 'claude', role_file: null } as HubSession;
    const w = mount(SessionRoleForm, { props: { session } });
    expect(w.get('.session-role-file-hint').text()).toContain(PIN_HINT);
    expect(w.text()).not.toContain('со следующего запуска процесса');
  });
});
