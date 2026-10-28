// Ф41-8 (интерфейс): каталог ролей хаба (`GET /roles`) для списка «Роль» в форме «Новая сессия». Ручки может не быть
// (404) — список тогда пуст, остаётся свободный текст.
import { reactive } from 'vue';

import * as api from '../api/client';
import type { RoleInfo } from '../api/types';

export const rolesState = reactive<{ items: RoleInfo[]; loaded: boolean; gone: boolean }>({ items: [], loaded: false, gone: false });

export function resetRoles(): void {
  rolesState.items = [];
  rolesState.loaded = false;
  rolesState.gone = false;
}

export async function loadRoles(): Promise<void> {
  if (rolesState.gone) return; // ручки нет в ядре — не стучимся, пока страница не перезагружена
  try {
    const res = await api.listRoles();
    rolesState.items = Array.isArray(res)
      ? res.filter((r) => r !== null && typeof r === 'object' && typeof r.key === 'string' && Array.isArray(r.names))
      : [];
  } catch (err) {
    if (err instanceof api.ApiError && err.status === 404) rolesState.gone = true;
    rolesState.items = [];
  } finally {
    rolesState.loaded = true;
  }
}
