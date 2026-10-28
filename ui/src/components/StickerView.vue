<script setup lang="ts">
// Стикер Telegram ≈160 px: .webp — <img>, .webm — <video autoplay loop muted playsinline>, .tgs — Lottie.
// `.tgs` — gzip с Lottie-JSON: распаковывается `DecompressionStream('gzip')`, библиотека (lottie_light — без
// eval, под CSP) грузится лениво отдельным чанком. Любая ошибка — просто текст «стикер 😀».
import { onBeforeUnmount, onMounted, ref } from 'vue';

import type { StickerKind } from '../render/media';

const props = defineProps<{ src: string; kind: StickerKind; emoji?: string }>();

const host = ref<HTMLElement | null>(null);
const failed = ref(false);
let destroy: (() => void) | null = null;
let disposed = false;

async function gunzipJson(src: string): Promise<unknown> {
  const response = await fetch(src, { credentials: 'same-origin' });
  if (!response.ok || response.body === null) throw new Error(`sticker http ${response.status}`);
  const stream = response.body.pipeThrough(new DecompressionStream('gzip'));
  return JSON.parse(await new Response(stream).text()) as unknown;
}

async function startLottie(): Promise<void> {
  try {
    const [data, mod] = await Promise.all([gunzipJson(props.src), import('lottie-web/build/player/lottie_light')]);
    if (disposed || host.value === null) return;
    const lottie = mod.default;
    const anim = lottie.loadAnimation({
      container: host.value,
      renderer: 'svg',
      loop: true,
      autoplay: true,
      animationData: data,
    });
    destroy = () => anim.destroy();
  } catch (err) {
    console.error('sticker lottie failed', err);
    failed.value = true;
  }
}

onMounted(() => {
  if (props.kind === 'lottie') void startLottie();
});

onBeforeUnmount(() => {
  disposed = true;
  destroy?.();
});
</script>

<template>
  <span
    class="sticker"
    :class="`sticker--${kind}`"
  >
    <span
      v-if="failed"
      class="sticker__text"
    >стикер {{ emoji ?? '' }}</span>
    <img
      v-else-if="kind === 'img'"
      class="sticker__media"
      :src="src"
      :alt="`стикер ${emoji ?? ''}`.trim()"
      loading="lazy"
      @error="failed = true"
    >
    <video
      v-else-if="kind === 'video'"
      class="sticker__media"
      :src="src"
      muted
      autoplay
      loop
      playsinline
      @error="failed = true"
    />
    <span
      v-else
      ref="host"
      class="sticker__media sticker__lottie"
    />
  </span>
</template>

<style scoped>
.sticker {
  display: inline-block;
}

.sticker__media {
  display: block;
  width: 160px;
  height: 160px;
  object-fit: contain;
}

.sticker__text {
  color: var(--muted-fg);
}
</style>
