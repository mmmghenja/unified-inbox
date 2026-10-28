// Ф36-2 B3: состояние поля ввода диалога с агентом — живёт в родителе, а не в размонтируемом диалоге.
export interface AgentDraft {
  text: string;
  error: string | null;
  busy: boolean;
}
