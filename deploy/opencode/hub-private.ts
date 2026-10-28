// Плагин хаба для ПРИВАТНЫХ серверов OpenCode (дежурная, рабочие сессии, оркестратор; Ф16, П9, F19).
// Подключается сгенерированным конфигом сервера (`"plugin": ["file://…/deploy/opencode/hub-private.ts"]`),
// в OpenCode владельца НЕ ставится. При загрузке убирает пароль сервера из окружения процесса OpenCode: команды bash
// модели и MCP-дети его больше не наследуют, а basic auth сервера продолжает работать (проверено живьём, probe7).
export const HubPrivate = async () => {
  delete process.env.OPENCODE_SERVER_PASSWORD
  return {}
}
