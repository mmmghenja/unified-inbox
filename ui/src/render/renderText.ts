// Текст + `entities_json` -> VNode. Спека, раздел «Уточнения архитектора»: «ТОЛЬКО текстовые узлы и белый
// список обёрток strong/em/code/pre/a/span». `h()` из vue — единственный допустимый способ строить разметку
// для недоверенного текста (без директивы, вставляющей сырой HTML, и без шаблона-строки).
import { h, type VNode, type VNodeChild } from 'vue';

import { parseEntities, safeHref, type TextEntity } from './entities';

function wrap(entity: TextEntity, textSlice: string): VNodeChild {
  switch (entity.type) {
    case 'bold':
      return h('strong', textSlice);
    case 'italic':
      return h('em', textSlice);
    case 'code':
      return h('code', textSlice);
    case 'pre':
      return h('pre', textSlice);
    case 'mention':
      return h('span', { class: 'entity-mention' }, textSlice);
    case 'link': {
      const href = entity.url !== undefined ? safeHref(entity.url) : null;
      if (href === null) return textSlice; // небезопасная/нераспознанная схема — простой текст, без <a>
      return h(
        'a',
        { href, rel: 'noopener noreferrer', target: '_blank' },
        textSlice,
      );
    }
    default:
      return textSlice;
  }
}

/** Построить дочерние узлы одного текстового блока: чередование строковых (текстовых) кусков и обёрток
 * из белого списка. Никогда не порождает произвольный тег — только те, что перечислены в `wrap()`. */
export function buildTextChildren(text: string, entitiesJson: string | null): VNodeChild[] {
  const entities = parseEntities(entitiesJson, text.length);
  if (entities.length === 0) return [text];

  const children: VNodeChild[] = [];
  let cursor = 0;
  for (const entity of entities) {
    if (entity.offset > cursor) {
      children.push(text.slice(cursor, entity.offset));
    }
    children.push(wrap(entity, text.slice(entity.offset, entity.offset + entity.length)));
    cursor = entity.offset + entity.length;
  }
  if (cursor < text.length) {
    children.push(text.slice(cursor));
  }
  return children;
}

/** Готовый VNode для сообщения ленты: `<span class="msg-text">…</span>` с текстовыми узлами и обёртками
 * внутри. Используется через компонент `src/components/MessageText.ts`, а не вызывается из шаблонов
 * напрямую — так рендер тестируется (`tests/08-*.spec.ts`) отдельно от Vue-компонентов. */
export function renderMessageText(text: string, entitiesJson: string | null): VNode {
  return h('span', { class: 'msg-text' }, buildTextChildren(text, entitiesJson));
}
