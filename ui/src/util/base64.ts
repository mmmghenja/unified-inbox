// Ф56-5б: base64 для кадров терминала (`data_b64`) — байты без потерь, без зависимостей.

/** base64 -> байты (неверная строка — пустой массив). */
export function b64ToBytes(b64: string): Uint8Array {
  try {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return new Uint8Array(0);
  }
}

/** Байты -> base64 (кусками: `btoa(String.fromCharCode(...big))` на больших массивах падает). */
export function bytesToB64(bytes: Uint8Array): string {
  let bin = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

const encoder = new TextEncoder();

/** Строка (ввод xterm `onData`) -> UTF-8 байты. */
export function textToBytes(text: string): Uint8Array {
  return encoder.encode(text);
}

/** Бинарная строка xterm (`onBinary`, символы 0–255) -> байты. */
export function binaryToBytes(text: string): Uint8Array {
  const out = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i += 1) out[i] = text.charCodeAt(i) & 0xff;
  return out;
}
