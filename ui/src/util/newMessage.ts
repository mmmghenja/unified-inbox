// Ф14-2 D2 (`docs/architecture.md`): чистая логика формы «Новое сообщение» — разбор ввода
// «Новый адресат», выбор «Откуда», сборка тела `POST /channels/new`. Компонент (`NewMessageForm.vue`) только
// держит состояние и рисует.

import type { ChannelNewInput } from '../api/client';
import type { Account, NewChannelTo, RecipientPerson } from '../api/types';

/** Как в ядре (`new_recipient.parse_tg_username`): латиница, цифры, `_`, 4–32 символа, с буквы. */
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_]{3,31}$/;
const TME_RE = /^(?:https:\/\/)?t\.me\/([^/?#\s]+)$/i;
/** Как в ядре (`new_recipient._EMAIL_RE`): ровно один адрес `local@domain`. */
const EMAIL_RE =
  /^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$/;
const EMAIL_MAX = 254;

/** Людей Slack в одной новой беседе (групповая личка) — не больше. */
export const SLACK_MAX_PEOPLE = 8;
/** Ф40-4: людей Mattermost в одной новой беседе (групповая личка) — не больше (сервер: 7 + я). */
export const MATTERMOST_MAX_PEOPLE = 7;

/** Потолок людей в новой групповой беседе по источнику (`slack` — 8, `mattermost` — 7). */
export function groupMaxPeople(source: 'slack' | 'mattermost'): number {
  return source === 'mattermost' ? MATTERMOST_MAX_PEOPLE : SLACK_MAX_PEOPLE;
}
/** Поиск «Кому» — от двух символов, пауза 250 мс. */
export const RECIPIENT_MIN_CHARS = 2;
export const RECIPIENT_DEBOUNCE_MS = 250;

export type NewAddress =
  | { kind: 'telegram'; username: string }
  | { kind: 'mail'; address: string }
  | { kind: 'tg_phone'; phone: string };

/** Ф34-3: номер — строка из `+`, цифр, пробелов, скобок и дефисов, начинается с `+`, `(` или цифры, цифр 8–15
 * (как в ядре; нормализует и проверяет оно же). */
const PHONE_CHARS_RE = /^[+(\d][\d\s()+-]*$/;
export function looksLikePhone(input: string): boolean {
  const value = input.trim();
  if (value === '' || !PHONE_CHARS_RE.test(value)) return false;
  if (value.lastIndexOf('+') > 0) return false;
  const digits = value.replace(/\D/g, '').length;
  return digits >= 8 && digits <= 15;
}

/** «Новый адресат» из самого ввода: `@name` / `t.me/name` -> Telegram, адрес почты -> письмо; иначе `null`
 * (просто имя без `@` — это поиск, а не адрес). */
export function parseNewAddress(input: string): NewAddress | null {
  const value = input.trim();
  if (value === '') return null;
  const tme = TME_RE.exec(value);
  if (tme !== null || value.startsWith('@')) {
    const name = tme !== null ? tme[1]! : value.slice(1);
    return USERNAME_RE.test(name) ? { kind: 'telegram', username: name } : null;
  }
  if (value.length <= EMAIL_MAX && EMAIL_RE.test(value)) return { kind: 'mail', address: value };
  if (looksLikePhone(value)) return { kind: 'tg_phone', phone: value };
  return null;
}

export interface SlackPick {
  id: string;
  name: string;
}

/** Выбранный адресат формы. */
export type Target =
  | { type: 'person'; person: RecipientPerson }
  | { type: 'slack'; accountId: number; people: SlackPick[] }
  | { type: 'mattermost'; accountId: number; people: SlackPick[] }
  | { type: 'new'; address: NewAddress };

/** Человек Slack из выдачи — id из `to` (`slack_users` с одним человеком); иначе `null`. */
export function slackPickOf(person: RecipientPerson): SlackPick | null {
  if (person.source !== 'slack' || typeof person.to !== 'object' || person.to.kind !== 'slack_users') return null;
  const id = person.to.users[0];
  if (typeof id !== 'string' || id === '') return null;
  return { id, name: person.name || person.handle || id };
}

/** Ф40-4: человек Mattermost из выдачи — id из `to` (`mm_users` с одним человеком); иначе `null`. */
export function mattermostPickOf(person: RecipientPerson): SlackPick | null {
  if (person.source !== 'mattermost' || typeof person.to !== 'object' || person.to.kind !== 'mm_users') return null;
  const id = person.to.users[0];
  if (typeof id !== 'string' || id === '') return null;
  return { id, name: person.name || person.handle || id };
}

/** Цель-«компания» (несколько людей одного аккаунта чипами): Slack или Mattermost. */
export function isGroupTarget(target: Target | null): target is Extract<Target, { type: 'slack' | 'mattermost' }> {
  return target?.type === 'slack' || target?.type === 'mattermost';
}

/** Выбор человека из списка: Slack/Mattermost того же аккаунта — добавляется чипом (Slack до 8, Mattermost до 7,
 * без повторов); остальное — заменяет адресата. */
export function pickPerson(current: Target | null, person: RecipientPerson): Target {
  const slack = slackPickOf(person);
  const pick = slack ?? mattermostPickOf(person);
  if (pick === null) return { type: 'person', person };
  const type = slack !== null ? 'slack' : 'mattermost';
  if (current?.type === type && current.accountId === person.account_id) {
    if (current.people.some((p) => p.id === pick.id) || current.people.length >= groupMaxPeople(type)) return current;
    return { ...current, people: [...current.people, pick] };
  }
  return { type, accountId: person.account_id, people: [pick] };
}

/** Убрать человека крестиком (Slack или Mattermost); последний убран — адресата нет. */
export function removeSlackPerson(current: Target | null, id: string): Target | null {
  if (!isGroupTarget(current)) return current;
  const people = current.people.filter((p) => p.id !== id);
  return people.length === 0 ? null : { ...current, people };
}

/** Источник, с которого пишется адресату. */
export function targetSource(target: Target | null): string | null {
  if (target === null) return null;
  if (target.type === 'person') return target.person.source;
  if (target.type === 'slack' || target.type === 'mattermost') return target.type;
  return target.address.kind === 'mail' ? 'mail' : 'telegram';
}

export interface FromChoice {
  /** Аккаунты на выбор (у человека — ровно его аккаунт). */
  accounts: Account[];
  /** Подставленный аккаунт; `null` — выбор обязателен (или выбирать не из чего). */
  preset: number | null;
  /** `true` — аккаунт определён адресатом, выбора нет. */
  fixed: boolean;
}

/** «Откуда» по адресату (решение 2): у человека — его аккаунт; у нового адресата — аккаунты этого вида с
 * `new_message`, единственный подставляется, несколько — выбор обязателен. */
export function fromChoice(target: Target | null, accounts: readonly Account[]): FromChoice {
  if (target === null) return { accounts: [], preset: null, fixed: false };
  if (target.type !== 'new') {
    const accountId = target.type === 'person' ? target.person.account_id : target.accountId;
    const source = targetSource(target) ?? '';
    const known = accounts.find((a) => a.id === accountId);
    const account: Account =
      known ??
      ({
        id: accountId,
        source,
        title: null,
        status: 'ok',
        last_ok_ts: null,
        last_error: null,
        flood_until: null,
      } as Account);
    return { accounts: [account], preset: accountId, fixed: true };
  }
  const source = targetSource(target);
  const phone = target.address.kind === 'tg_phone';
  // Ф62-1: по номеру пишут и Telegram, и WhatsApp — «Откуда» из обоих.
  const fitting = accounts.filter(
    (a) =>
      (a.source === source || (phone && a.source === 'whatsapp')) &&
      a.new_message === true &&
      (!phone || a.tg_phone_daily === undefined || a.tg_phone_daily > 0),
  );
  return { accounts: fitting, preset: fitting.length === 1 ? fitting[0]!.id : null, fixed: false };
}

/** Подпись аккаунта в «Откуда»: почта — адрес, Telegram/Slack — «Telegram · <title>» / «Slack · <title>». */
export function accountLabel(item: Account): string {
  if (item.source === 'mail') return item.address || item.title || `Почта #${item.id}`;
  const name =
    item.source === 'slack'
      ? 'Slack'
      : item.source === 'telegram'
        ? 'Telegram'
        : item.source === 'mattermost'
          ? 'Mattermost'
          : item.source === 'whatsapp'
            ? 'WhatsApp'
            : item.source;
  return item.title ? `${name} · ${item.title}` : `${name} #${item.id}`;
}

/** Нужна ли тема (только почта). */
export function needsSubject(target: Target | null): boolean {
  return targetSource(target) === 'mail';
}

/** `to` для `channels/new`: у человека — объект или строка из выдачи; у Slack и Mattermost — все выбранные люди; у нового
 * адресата — `@name` / адрес. */
export function targetTo(target: Target): NewChannelTo {
  if (target.type === 'person') return target.person.to;
  if (target.type === 'slack') return { kind: 'slack_users', users: target.people.map((p) => p.id) };
  if (target.type === 'mattermost') return { kind: 'mm_users', users: target.people.map((p) => p.id) };
  if (target.address.kind === 'tg_phone') return target.address.phone;
  return target.address.kind === 'telegram' ? `@${target.address.username}` : target.address.address;
}

/** Значение `<select>` «Проект»: `''` — «по правилам», иначе id проекта. */
export function projectIdOf(value: string): number | null {
  const id = Number(value);
  return value !== '' && Number.isInteger(id) && id > 0 ? id : null;
}

/** Тело «Создать»; `null` — не хватает адресата, аккаунта или темы у письма. */
export function buildChannelNew(input: {
  target: Target | null;
  accountId: number | null;
  project: string;
  subject: string;
}): ChannelNewInput | null {
  const { target, accountId } = input;
  if (target === null || accountId === null) return null;
  const subject = input.subject.trim();
  const mail = needsSubject(target);
  if (mail && subject === '') return null;
  return {
    accountId,
    projectId: projectIdOf(input.project),
    to: targetTo(target),
    ...(mail ? { subject } : {}),
  };
}

/** Ф34-3: подсказка лимита поиска по номеру. Выбран аккаунт «Откуда» (`accountId`) — предел его источника
 * (у WhatsApp — `wa_phone_daily`, ядро отдаёт его тем же полем); не выбран — наибольший по Telegram- и
 * WhatsApp-аккаунтам (предлагать ли номер вообще); `null` — нет данных/аккаунтов. */
export function phoneLimitHint(
  accounts: readonly Account[],
  accountId: number | null = null,
): { daily: number; enabled: boolean } | null {
  const tg = accounts.filter(
    (a) =>
      (a.source === 'telegram' || a.source === 'whatsapp') &&
      a.new_message === true &&
      a.tg_phone_daily !== undefined &&
      (accountId === null || a.id === accountId),
  );
  if (tg.length === 0) return null;
  const daily = Math.max(...tg.map((a) => a.tg_phone_daily as number));
  return { daily, enabled: daily > 0 };
}

/** Подпись человека в списке: «имя · @handle» (без повторов). */
export function personLabel(person: RecipientPerson): string {
  const name = person.name?.trim() || '';
  const handle = person.handle?.trim() || '';
  if (name && handle && name !== handle) return `${name} · ${handle}`;
  return name || handle || (typeof person.to === 'string' ? person.to : '(без имени)');
}
