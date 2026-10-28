// Ф14-4 D1/D2 (docs/architecture.md): можно ли записывать голос в этом окне и в каком формате.
// Микрофон браузер даёт только в безопасном контексте (`localhost` на ноутбуке); через VPN с телефона (http) —
// нет, там вместо записи выбор аудиофайла (`<input type=file accept="audio/*" capture>`).

/** 10 минут — запись останавливается сама. */
export const VOICE_MAX_SECONDS = 600;

const OGG_OPUS = 'audio/ogg;codecs=opus';
const WEBM_OPUS = 'audio/webm;codecs=opus';

/** Решение 1: кнопка микрофона — если есть `MediaRecorder`, `getUserMedia` и безопасный контекст. */
export function canRecordVoice(win: Window & typeof globalThis = window): boolean {
  return (
    win.isSecureContext === true &&
    typeof win.MediaRecorder === 'function' &&
    typeof win.navigator?.mediaDevices?.getUserMedia === 'function'
  );
}

/** Формат записи: ogg/opus, если браузер его пишет (Firefox), иначе webm/opus (Chrome); `undefined` — пусть
 * браузер выберет сам (ядро всё равно примет только webm/ogg). */
export function pickVoiceMime(recorder: typeof MediaRecorder = MediaRecorder): string | undefined {
  const supported = (type: string): boolean => {
    try {
      return typeof recorder.isTypeSupported === 'function' && recorder.isTypeSupported(type);
    } catch {
      return false;
    }
  };
  if (supported(OGG_OPUS)) return OGG_OPUS;
  if (supported(WEBM_OPUS)) return WEBM_OPUS;
  return undefined;
}

/** Имя файла записи по фактическому типу рекордера. */
export function voiceFileName(mime: string): string {
  return mime.toLowerCase().includes('ogg') ? 'voice.ogg' : 'voice.webm';
}

/** `м:сс`. */
export function formatVoiceTime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}
