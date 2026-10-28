// Разбор `reactions_json` (спека 1d-1, п.4): список `{ emoji|custom, count, me }`. Строка под сообщением вида
// «👍 3 ❤️ 1», своя реакция (`me`) выделяется отдельным классом, `custom` рисуется как «◆». Вход недоверенный —
// невалидные элементы отбрасываются по одному, остальные показываются.
//
// Ф10-3 (П11): элемент хранит сырое значение (`raw` — символ эмодзи, `:name:` Slack или id своего эмодзи
// Telegram) — по нему чип ставит/снимает реакцию; `custom` (свой эмодзи Telegram) — не кнопка.
import palette from './reactionPalette.json';

export interface ParsedReaction {
  label: string;
  count: number;
  me: boolean;
  raw: string;
  custom: boolean;
  /** Ф23-2: имена поставивших (до 100, как прислал источник; своя реакция — не в списке, её даёт `me`). */
  by?: string[];
  /** Ф23-2: сколько поставивших без имени или сверх списка. */
  byMore?: number;
}

/** Ф10-3: источники, где владелец ставит реакции из хаба. Ф15-1 D: почта — только письма с `can_react`. */
export type ReactionSource = 'telegram' | 'slack' | 'mattermost' | 'mail' | 'whatsapp';

/** Ф10-3 П2 + Ф18-1: быстрые ряды и полные наборы («ещё») по источнику — `reactionPalette.json`, его же читает
 * тест коннекторов (`tests/connectors/test_reaction_palette.py`). Telegram — весь его набор (без U+FE0F, как его
 * шлёт Telegram; ✅ и 🔍 в нём нет), Slack — эмодзи с именем Slack в таблице коннектора (`slack/convert.py`,
 * `SLACK_REACTION_NAMES`), Mattermost (Ф40-4) — набор Slack без четырёх эмодзи, которых нет на сервере 11.x. Ф15-1 D: почта — ряд Gmail. Ряды выбраны по реакциям в переписке владельца. */
/** Ф62-1: WhatsApp — любой одиночный эмодзи; быстрый ряд — как в самом WhatsApp (плюс 🤝 и 🔥), полный — набор почты. */
const WHATSAPP_QUICK: readonly string[] = ['👍', '❤️', '😂', '😮', '😢', '🙏', '🤝', '🔥'];

export const QUICK_REACTIONS: Record<ReactionSource, readonly string[]> = {
  telegram: palette.telegram.quick,
  slack: palette.slack.quick,
  mattermost: palette.mattermost.quick,
  mail: palette.mail.quick,
  whatsapp: WHATSAPP_QUICK,
};

export const ALL_REACTIONS: Record<ReactionSource, readonly string[]> = {
  telegram: palette.telegram.all,
  slack: palette.slack.all,
  mattermost: palette.mattermost.all,
  mail: palette.mail.all,
  whatsapp: [...WHATSAPP_QUICK, ...palette.mail.all.filter((e: string) => !WHATSAPP_QUICK.includes(e))],
};

/** Ф18-1: сколько реакций «ходовых в этой беседе» показывать первыми в «ещё». */
export const CHANNEL_REACTIONS_MAX = 12;

export function isReactionSource(source: string | undefined): source is ReactionSource {
  return source === 'telegram' || source === 'slack' || source === 'mattermost' || source === 'mail' || source === 'whatsapp';
}

/** Сравнение эмодзи — без U+FE0F (`❤️` и `❤` — одна реакция). */
export function reactionKey(value: string): string {
  return value.replace(/️/g, '');
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseBy(item: Record<string, unknown>): { by?: string[]; byMore?: number } {
  const by = Array.isArray(item.by)
    ? item.by.filter((n): n is string => typeof n === 'string' && n.length > 0).slice(0, 100)
    : [];
  const more = item.by_more;
  const byMore = typeof more === 'number' && Number.isInteger(more) && more > 0 ? more : 0;
  const out: { by?: string[]; byMore?: number } = {};
  if (by.length > 0) out.by = by;
  if (byMore > 0) out.byMore = byMore;
  return out;
}

export function parseReactions(reactionsJson: string | null): ParsedReaction[] {
  if (!reactionsJson) return [];
  let raw: unknown;
  try {
    raw = JSON.parse(reactionsJson);
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];

  const result: ParsedReaction[] = [];
  for (const item of raw) {
    if (!isPlainObject(item)) continue;
    const count = item.count;
    if (typeof count !== 'number' || !Number.isInteger(count) || count <= 0) continue;

    let parsed: ParsedReaction | null = null;
    if (typeof item.emoji === 'string' && item.emoji.length > 0) {
      parsed = { label: item.emoji, count, me: item.me === true, raw: item.emoji, custom: false, ...parseBy(item) };
    } else if (typeof item.custom === 'string' && item.custom.length > 0) {
      parsed = { label: '◆', count, me: item.me === true, raw: item.custom, custom: true, ...parseBy(item) };
    }
    if (parsed === null) continue;
    result.push(parsed);
  }
  return result;
}

/** Ф18-1: реакции, которые ставят в этой беседе (загруженные сообщения ленты), — по убыванию числа. Slack — и
 * свои эмодзи воркспейса (`:name:`, ставятся по имени); Telegram — только из его набора (свои эмодзи Telegram
 * не кнопки). Почта — нет. */
export function channelReactions(
  source: ReactionSource,
  reactionsJsons: readonly (string | null)[],
  limit = CHANNEL_REACTIONS_MAX,
): string[] {
  if (source === 'mail') return [];
  const allowed = source === 'telegram' ? new Set(ALL_REACTIONS.telegram.map(reactionKey)) : null;
  const totals = new Map<string, { raw: string; count: number }>();
  for (const json of reactionsJsons) {
    for (const r of parseReactions(json)) {
      if (r.custom) continue;
      const key = reactionKey(r.raw);
      if (allowed !== null && !allowed.has(key)) continue;
      const entry = totals.get(key);
      if (entry === undefined) totals.set(key, { raw: r.raw, count: r.count });
      else entry.count += r.count;
    }
  }
  return [...totals.values()]
    .sort((a, b) => b.count - a.count)
    .slice(0, limit)
    .map((e) => e.raw);
}

/** Ф18-1: «ещё» — сначала ходовые в беседе, затем весь набор источника; без быстрого ряда и без повторов (по
 * ключу без U+FE0F). */
export function moreReactionsFor(source: ReactionSource, inChannel: readonly string[]): string[] {
  const seen = new Set(QUICK_REACTIONS[source].map(reactionKey));
  const out: string[] = [];
  for (const emoji of [...inChannel, ...ALL_REACTIONS[source]]) {
    const key = reactionKey(emoji);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(emoji);
  }
  return out;
}

/** Свой эмодзи воркспейса Slack (`:name:`) — в сетке текстом. */
export function isNamedReaction(value: string): boolean {
  return /^:[^:\s]+:$/.test(value);
}
