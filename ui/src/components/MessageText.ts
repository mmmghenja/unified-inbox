// Компонент-обёртка над `render/renderText.ts` (спека: «рендер... через h() или компонент на слотах»).
// Намеренно НЕ `.vue`-файл с шаблоном: сам рендер отдаёт готовый VNode, шаблону здесь нечего компилировать.
// Ф40-4 A1: `markdown` (беседа Mattermost) — текст идёт через безопасный Markdown-рендерер (`ModelText`), а не
// через `entities_json`: у Mattermost разметка живёт в самом тексте.
import { defineComponent, h, type PropType } from 'vue';

import { renderMessageText } from '../render/renderText';
import ModelText from './ModelText.vue';

export default defineComponent({
  name: 'MessageText',
  props: {
    text: { type: String, required: true },
    entitiesJson: { type: String as PropType<string | null>, default: null },
    markdown: { type: Boolean, default: false },
  },
  setup(props) {
    return () =>
      props.markdown
        ? h('div', { class: 'msg-text msg-text--md' }, [h(ModelText, { text: props.text })])
        : renderMessageText(props.text, props.entitiesJson);
  },
});
