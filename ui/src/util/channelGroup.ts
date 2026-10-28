import type { Channel, ChannelKind } from '../api/types';

// Группы каналов внутри проекта (план, раздел 6: «каналы проекта (группы «Чаты / Почта / Задачи» по kind)»).
export type ChannelGroup = 'Чаты' | 'Почта' | 'Задачи';

const GROUP_BY_KIND: Record<ChannelKind, ChannelGroup> = {
  dm: 'Чаты',
  group: 'Чаты',
  channel: 'Чаты',
  topic: 'Чаты',
  mail_thread: 'Почта',
  issue: 'Задачи',
  card: 'Задачи',
  task: 'Задачи',
};

export const CHANNEL_GROUP_ORDER: ChannelGroup[] = ['Чаты', 'Почта', 'Задачи'];

export function groupOf(kind: ChannelKind): ChannelGroup {
  return GROUP_BY_KIND[kind];
}

// Ф8-6 C3: две группы списка бесед проекта — «Реагирую» (режим `react` и всё с `unread_react > 0`) сверху и
// «К сведению» (остальное: «к сведению», «сводка», «разбор моделью», заглушённые). Старое ядро без поля
// `attention` — беседа в «Реагирую» (как раньше: список целиком виден).
export type AttentionGroup = 'react' | 'info';

export function attentionGroupOf(channel: Channel): AttentionGroup {
  if ((channel.unread_react ?? 0) > 0) return 'react';
  if (channel.attention === undefined || channel.attention === 'react') return 'react';
  return 'info';
}

export function splitByAttention(channels: Channel[]): Record<AttentionGroup, Channel[]> {
  const result: Record<AttentionGroup, Channel[]> = { react: [], info: [] };
  for (const channel of channels) result[attentionGroupOf(channel)].push(channel);
  return result;
}

export function groupChannels(channels: Channel[]): Map<ChannelGroup, Channel[]> {
  const groups = new Map<ChannelGroup, Channel[]>();
  for (const group of CHANNEL_GROUP_ORDER) groups.set(group, []);
  for (const channel of channels) {
    if (channel.archived) continue;
    groups.get(groupOf(channel.kind))?.push(channel);
  }
  return groups;
}
