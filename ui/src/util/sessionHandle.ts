// Ф41-6: то, что окно сессии отдаёт наружу (`defineExpose`), чтобы действия жили в меню «⋯» шапки столбца или окна на
// телефоне, а формы и подтверждения оставались прежними — внутри окна.
export interface SessionWindowHandle {
  busy: boolean;
  canTerminal: boolean;
  nothingToSleep: boolean;
  sleep(): Promise<void>;
  wake(): Promise<void>;
  compact(): Promise<void>;
  mcpReload(): Promise<void>;
  returnMain(): Promise<void>;
  close(): Promise<void>;
  openPanel(panel: 'model' | 'role' | 'handoff' | 'interview'): void;
  openRoleText(): void;
  openCompact(): void;
  /** Ф99-7.2: «Разрешения…» — «Всегда в этой сессии» списком с «×». */
  openAlways(): void;
  openTerminal(): Promise<void>;
  toggleSubEdit(): void;
  /** Ф90-1: режим «Ручной | Авто» — в «⋯» и чипом «авто» в шапке (подтверждение включения — в окне). */
  chooseMode(next: 'manual' | 'auto'): Promise<void>;
  isAuto: boolean;
}
