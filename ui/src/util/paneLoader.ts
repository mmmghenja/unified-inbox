// Загрузчик ленивого `TerminalPane` вынесен в объект, чтобы тест мог подменить `load` (оборванный чанк через VPN).
export const paneLoader = {
  load: (): Promise<unknown> => import('../components/TerminalPane.vue'),
};
