<script setup lang="ts">
// Вложения сообщения или альбома (спека 1d-1, п.1–2; Ф8-5 B). Что показать на месте каждого вложения, решает
// чистая `render/media.ts` по `media` ядра и `detected` (сигнатура байтов): готовые картинки — превью в ленте
// (альбом — сеткой), клик — просмотр во весь экран; видео/аудио — плеер; прочее — «<имя> · <размер> · Скачать»;
// не скачанное — «Загрузить (<размер>)», статусы — надписями. Ссылок «открыть в Telegram» больше нет (B5).
// Общий компонент — MessageRow (одно сообщение) и AlbumRow (вложения всех сообщений альбома).
import { computed, reactive, ref } from 'vue';

import { ApiError, mediaUrl } from '../api/client';
import type { Message } from '../api/types';
import { formatAttachmentLabel, formatSize, parseAttachments, typeLabel } from '../render/attachments';
import {
  buildAttachmentViews,
  previewSize,
  sourceLabel,
  stickerKind,
  type AttachmentView,
  type StickerKind,
} from '../render/media';
import {
  foldTranscript,
  transcribeErrorText,
  transcriptView,
  type TranscriptView,
} from '../render/transcript';
import { store } from '../store/instance';
import MediaViewer from './MediaViewer.vue';
import StickerView from './StickerView.vue';
import TranscriptModal from './TranscriptModal.vue';

const props = defineProps<{ messages: Message[] }>();

const source = computed<string | null>(() => {
  const first = props.messages[0];
  if (first === undefined) return null;
  return store.state.sync.channelsById[first.channel_id]?.source ?? null;
});

const views = computed<AttachmentView[]>(() =>
  props.messages.flatMap((m) =>
    buildAttachmentViews(m.id, parseAttachments(m.attachments_json), m.media, source.value),
  ),
);

function boxOf(view: AttachmentView): { width: number; height: number } | null {
  return previewSize(view.media?.width, view.media?.height);
}

// В сетке — готовые картинки и заглушки «загружается…» с известными пропорциями (лента не прыгает, B4).
const gridViews = computed(() =>
  views.value.filter((v) => v.display === 'image' || (v.display === 'loading' && boxOf(v) !== null)),
);
const listViews = computed(() => views.value.filter((v) => !gridViews.value.includes(v)));
const isAlbum = computed(() => gridViews.value.length > 1);
// Альбом — сетка 2–3 в ряд (B2): две и четыре картинки — по две, остальное — по три.
const gridClass = computed(() => ({
  'media-grid--album': isAlbum.value,
  'media-grid--two': gridViews.value.length === 2 || gridViews.value.length === 4,
}));
const images = computed(() => views.value.filter((v) => v.display === 'image'));

function srcOf(view: AttachmentView): string {
  return view.media === null ? '' : mediaUrl(view.media.id);
}

function stickerKindOf(view: AttachmentView): StickerKind | null {
  return view.media === null ? null : stickerKind(view.att, view.media.detected ?? 'other');
}

function altOf(view: AttachmentView): string {
  return view.att.name ?? typeLabel(view.att.type);
}

function boxStyle(view: AttachmentView): Record<string, string> {
  if (isAlbum.value) return {};
  const box = boxOf(view);
  return box === null ? {} : { width: `${box.width}px`, height: `${box.height}px` };
}

const viewerStart = ref<number | null>(null);
const viewerItems = computed(() => images.value.map((v) => ({ src: srcOf(v), alt: altOf(v) })));

function openViewer(view: AttachmentView): void {
  const index = images.value.indexOf(view);
  if (index !== -1) viewerStart.value = index;
}

function fileLabel(view: AttachmentView): string {
  const name = view.att.name ?? typeLabel(view.att.type);
  return view.size !== null ? `${name} · ${formatSize(view.size)}` : name;
}

function fetchLabel(view: AttachmentView): string {
  return view.size !== null ? `Загрузить (${formatSize(view.size)})` : 'Загрузить';
}

// Кнопка на время запроса гаснет; ошибка запроса — надпись у вложения (текст ошибки — в консоли).
const busy = reactive(new Set<string>());
const failedRequest = reactive(new Set<string>());

function keyOf(view: AttachmentView): string {
  return `${view.messageId}:${view.att.idx}`;
}

async function requestFetch(view: AttachmentView): Promise<void> {
  const key = keyOf(view);
  if (busy.has(key)) return;
  busy.add(key);
  failedRequest.delete(key);
  try {
    await store.fetchMedia(view.messageId, view.att.idx);
  } catch (err) {
    failedRequest.add(key);
    console.error('media fetch failed', err);
  } finally {
    busy.delete(key);
  }
}

// Ф14-6b: расшифровка голосового под плеером (аудио и видео-кружок). Текст — только интерполяцией.
const transcripts = computed(
  () => new Map<string, TranscriptView>(views.value.map((v) => [keyOf(v), transcriptView(v.att, v.media)])),
);
// Ф99-6: «показать целиком» — окном поверх, а не раскрытием в ленте.
const fullView = ref<AttachmentView | null>(null);
const transcribeError = reactive(new Map<string, string>());

function tv(view: AttachmentView): TranscriptView {
  return transcripts.value.get(keyOf(view)) ?? transcriptView(view.att, view.media);
}

function showTranscript(view: AttachmentView): boolean {
  if (view.display !== 'audio' && view.display !== 'video') return false;
  const t = tv(view);
  return t.state !== 'none' || t.button !== null || transcribeError.has(keyOf(view));
}

function transcriptText(view: AttachmentView): string {
  const text = tv(view).text;
  return foldTranscript(text).short;
}

function isFolded(view: AttachmentView): boolean {
  return foldTranscript(tv(view).text).folded;
}

function openFull(view: AttachmentView): void {
  fullView.value = view;
}

async function requestTranscribe(view: AttachmentView): Promise<void> {
  const key = keyOf(view);
  if (view.media === null || busy.has(key)) return;
  busy.add(key);
  transcribeError.delete(key);
  try {
    await store.transcribeMedia(view.media.id);
  } catch (err) {
    const status = err instanceof ApiError ? err.status : null;
    const detail = err instanceof ApiError ? err.message : null;
    transcribeError.set(key, transcribeErrorText(status, detail));
    console.error('transcribe failed', err);
  } finally {
    busy.delete(key);
  }
}
</script>

<template>
  <div
    v-if="views.length > 0"
    class="media-block"
  >
    <div
      v-if="gridViews.length > 0"
      class="media-grid"
      :class="gridClass"
    >
      <template
        v-for="view in gridViews"
        :key="`${view.messageId}:${view.att.idx}`"
      >
        <button
          v-if="view.display === 'image'"
          type="button"
          class="media-thumb"
          :style="boxStyle(view)"
          @click="openViewer(view)"
        >
          <img
            class="media-img"
            :src="srcOf(view)"
            :alt="altOf(view)"
            loading="lazy"
          >
        </button>
        <div
          v-else
          class="media-placeholder"
          :style="boxStyle(view)"
        >
          загружается…
        </div>
      </template>
    </div>
    <ul
      v-if="listViews.length > 0"
      class="attachments"
    >
      <li
        v-for="view in listViews"
        :key="`${view.messageId}:${view.att.idx}`"
        class="attachment"
        :class="`attachment--${view.display}`"
      >
        <StickerView
          v-if="view.display === 'sticker' && view.media !== null && stickerKindOf(view) !== null"
          :src="srcOf(view)"
          :kind="stickerKindOf(view)!"
          :emoji="view.att.emoji"
        />
        <video
          v-else-if="view.display === 'video'"
          class="media-video"
          :src="srcOf(view)"
          :style="boxStyle(view)"
          controls
          preload="metadata"
        />
        <video
          v-else-if="view.display === 'loop'"
          class="media-video"
          :src="srcOf(view)"
          :style="boxStyle(view)"
          muted
          autoplay
          loop
          playsinline
        />
        <audio
          v-else-if="view.display === 'audio'"
          class="media-audio"
          :src="srcOf(view)"
          controls
          preload="metadata"
        />
        <template v-else-if="view.display === 'file'">
          <span>📎 {{ fileLabel(view) }} ·</span>
          <a
            class="media-download"
            :href="srcOf(view)"
            download
          >Скачать</a>
        </template>
        <template v-else>
          <span>📎 {{ formatAttachmentLabel(view.att) }}</span>
          <span
            v-if="view.display === 'loading'"
            class="media-status"
          >загружается…</span>
          <button
            v-else-if="view.display === 'fetch'"
            type="button"
            class="media-fetch"
            :disabled="busy.has(keyOf(view))"
            @click="requestFetch(view)"
          >
            {{ fetchLabel(view) }}
          </button>
          <template v-else-if="view.display === 'failed'">
            <span class="media-status">не удалось загрузить</span>
            <button
              type="button"
              class="media-fetch"
              :disabled="busy.has(keyOf(view))"
              @click="requestFetch(view)"
            >
              Повторить
            </button>
          </template>
          <span
            v-else-if="view.display === 'gone'"
            class="media-status"
          >файла больше нет в {{ sourceLabel(source) }}</span>
          <span
            v-else-if="view.display === 'too_big'"
            class="media-status"
          >слишком большой{{ view.size !== null ? ` (${formatSize(view.size)})` : '' }}</span>
          <span
            v-else-if="view.display === 'ttl'"
            class="media-status"
          >одноразовое — смотри в Telegram</span>
          <span
            v-else-if="view.display === 'external'"
            class="media-status"
          >внешний файл — открыть в Slack</span>
          <span
            v-if="failedRequest.has(keyOf(view))"
            class="media-status media-status--error"
          >не получилось, попробуй ещё раз</span>
        </template>
        <div
          v-if="showTranscript(view)"
          class="transcript"
        >
          <span
            v-if="tv(view).state === 'working'"
            class="transcript__status"
          >расшифровываю…</span>
          <span
            v-else-if="tv(view).state === 'failed'"
            class="transcript__status"
          >не удалось расшифровать</span>
          <span
            v-else-if="tv(view).state === 'silence'"
            class="transcript__status"
          >речи не слышно</span>
          <template v-else-if="tv(view).state === 'done'">
            <p class="transcript__text">
              <span
                v-if="tv(view).langMark"
                class="transcript__lang"
              >{{ tv(view).langMark }} · </span>{{ transcriptText(view) }}
            </p>
            <button
              v-if="isFolded(view)"
              type="button"
              class="transcript__more"
              @click="openFull(view)"
            >
              показать целиком
            </button>
            <span
              v-if="tv(view).doubtful"
              class="transcript__status"
            >расшифровка может быть неточной</span>
          </template>
          <button
            v-if="tv(view).button"
            type="button"
            class="transcript__action"
            :disabled="busy.has(keyOf(view))"
            @click="requestTranscribe(view)"
          >
            {{ tv(view).button }}
          </button>
          <span
            v-if="transcribeError.has(keyOf(view))"
            class="media-status media-status--error"
          >{{ transcribeError.get(keyOf(view)) }}</span>
        </div>
      </li>
    </ul>
    <MediaViewer
      v-if="viewerStart !== null"
      :items="viewerItems"
      :start="viewerStart"
      @close="viewerStart = null"
    />
    <TranscriptModal
      v-if="fullView !== null"
      :title="fullView.att.name ? `Расшифровка: ${fullView.att.name}` : 'Расшифровка'"
      :text="tv(fullView).text"
      @close="fullView = null"
    />
  </div>
</template>
