// Владелец 12.10.2026: в ленте сессии последний ответ не сворачивается (прежние — свёрнуты); разрешение в окне
// сессии свёрнуто до начала, «развернуть» показывает всё, «открыть окном» — целиком поверх.
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, describe, expect, it } from 'vitest';

import type { SessionRequest } from '../src/api/types';
import ModelText from '../src/components/ModelText.vue';
import WorkEventFeed from '../src/components/WorkEventFeed.vue';
import WorkRequestCard from '../src/components/WorkRequestCard.vue';

// Ф78-1: карточка получает решение, повтор и «убрать» от хозяина — в этих тестах они не вызываются.
const HANDLERS = { decide: async () => 'ok' as const, retry: async () => 'ok' as const, dismiss: () => undefined };

const ev = (id: number, kind: string, summary: string, ref: string | null = null) =>
  ({ id, kind, ts: 1, summary, tool: null, is_error: false, ref }) as never;

function request(over: Partial<SessionRequest> = {}): SessionRequest {
  return {
    id: 1,
    session_id: 5,
    project_id: 3,
    kind: 'permission',
    tool: 'Bash',
    input: { command: 'git push origin dev\necho вторая\necho третья', description: 'пуш в dev' },
    input_truncated: false,
    reason: 'push под кнопкой',
    status: 'pending',
    answer: null,
    created_ts: 1,
    decided_ts: null,
    delivered: false,
    ...over,
  } as SessionRequest;
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('лента сессии: последний ответ целиком', () => {
  it('последний `text` не сворачивается и помечен ключом ответа; прежние — сворачиваемые', async () => {
    const w = mount(WorkEventFeed, {
      props: {
        events: [ev(1, 'text', 'первый'), ev(2, 'tool', 'ls'), ev(3, 'text', 'итог', 'blk-7'), ev(4, 'turn_end', '9 с')],
        olderDone: true,
        load: async () => undefined,
        sessionId: 5,
      },
    });
    await flushPromises();
    const texts = w.findAllComponents(ModelText);
    expect(texts.map((t) => [t.props('text'), t.props('collapsible')])).toEqual([
      ['первый', true],
      ['итог', false],
    ]);
    expect(w.find('[data-event-id="3"]').attributes('data-answer-key')).toBe('blk-7');
    expect(w.find('[data-event-id="1"]').attributes('data-answer-key')).toBeUndefined();
    expect(w.find('[data-event-id="4"]').attributes('data-kind')).toBe('turn_end');

    // пришёл новый ответ — прежний «последний» становится сворачиваемым
    await w.setProps({ events: [ev(3, 'text', 'итог', 'blk-7'), ev(5, 'text', 'новый')] });
    expect(w.findAllComponents(ModelText).map((t) => t.props('collapsible'))).toEqual([true, false]);
    expect(w.find('[data-event-id="5"]').attributes('data-answer-key')).toBe('5');
    w.unmount();
  });
});

describe('карточка разрешения в окне сессии', () => {
  it('compact: свёрнута до начала команды; «развернуть» — описание и причина; «открыть окном» — целиком', async () => {
    const w = mount(WorkRequestCard, { props: { ...HANDLERS, request: request(), compact: true }, attachTo: document.body });
    expect(w.get('[data-test="request-preview"]').text()).toContain('git push origin dev');
    expect(w.find('.work-request-description').exists()).toBe(false);
    expect(w.find('.work-request-reason').exists()).toBe(false);
    expect(w.find('button.work-allow').exists()).toBe(true); // решить можно, не разворачивая

    await w.get('button.work-request-expand').trigger('click');
    expect(w.find('[data-test="request-preview"]').exists()).toBe(false);
    expect(w.get('.work-request-description').text()).toBe('пуш в dev');
    expect(w.get('.work-request-reason').text()).toContain('push под кнопкой');
    expect(w.get('button.work-request-expand').text()).toBe('свернуть');

    await w.get('button.work-request-open').trigger('click');
    const modal = document.body.querySelector('.request-text-modal')!;
    expect(modal.querySelector('.request-text-command')!.textContent).toContain('echo третья');
    expect(modal.textContent).toContain('причина: push под кнопкой');
    (modal.querySelector('.channel-modal-close') as HTMLButtonElement).click();
    await flushPromises();
    expect(document.body.querySelector('.request-text-modal')).toBeNull();
    w.unmount();
  });

  it('без compact (диспетчерская) и вопрос сессии — как прежде, без сворачивания', () => {
    const plain = mount(WorkRequestCard, { props: { ...HANDLERS, request: request() } });
    expect(plain.find('[data-test="request-preview"]').exists()).toBe(false);
    expect(plain.find('button.work-request-expand').exists()).toBe(false);
    expect(plain.get('.work-request-reason').text()).toContain('push под кнопкой');
    const question = mount(WorkRequestCard, {
      props: { ...HANDLERS, request: request({ kind: 'question', tool: 'AskUserQuestion', input: { questions: [] } }), compact: true },
    });
    expect(question.find('button.work-request-expand').exists()).toBe(false);
    expect(question.find('[data-test="request-preview"]').exists()).toBe(false);
  });

  it('вход без команды — в свёрнутом виде начало входа инструмента', () => {
    const w = mount(WorkRequestCard, {
      props: { ...HANDLERS, request: request({ tool: 'mcp__jira__transition', input: { issue: 'CLIENTMOB-133' } }), compact: true },
    });
    expect(w.get('[data-test="request-preview"]').text()).toContain('CLIENTMOB-133');
  });
});
