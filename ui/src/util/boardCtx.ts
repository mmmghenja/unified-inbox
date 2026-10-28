// Ф41-6б: ключ provide/inject — компонент ленты стоит внутри столбца беседы на «Главной». Тогда «в тред» и
// «Ответить» (Slack/Mattermost) не открывают панель треда на месте, а переводят на вкладку проекта (2.4).
import type { InjectionKey } from 'vue';

export const IN_BOARD_COLUMN: InjectionKey<boolean> = Symbol('in-board-column');
