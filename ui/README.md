# hub-ui

Каркас UI (Vue 3 + Vite + TypeScript) для `hub` — кусок фазы 0 №6b. Раздаёт собранный `dist/` ядро
(`hub core --ui-dir`), отдельного dev-сервера Vite в рабочем процессе нет (см. `docs/architecture.md`).

## Node

Нужен Node 24; в обычном login-шелле по умолчанию Node 22 (`nvm`). Каждую команду запускать так:

```sh
source ~/.nvm/nvm.sh && nvm use 24
```

(если версии нет: `nvm install 24`). В каталоге лежит `.nvmrc` — можно `nvm use` без номера.

## Команды

Все — из каталога `ui/`:

```sh
npm ci                # установить зависимости (package-lock.json в репозитории)
npm run build          # vue-tsc --noEmit && vite build -> ui/dist
npm run watch          # vite build --watch -- пересборка при разработке, dev-сервера нет
npm run lint           # eslint .
npm run typecheck      # vue-tsc --noEmit
npm test               # vitest run
```

`ui/dist` раздаёт ядро (`hub core --ui-dir ui/dist`, по умолчанию именно этот путь). Живой стенд — через
`scripts/dev-demo.py` (кусок 6a), не через эту сборку напрямую.

## Структура

- `src/api/` — клиент API (`client.ts`, один модуль на всё) и типы ответов (`types.ts`), выведенные из
  `docs/architecture.md` и сверенные с `src/hub/core/queries.py`/`routes/*.py`.
- `src/store/` — состояние на `reactive` (без pinia, без роутера): `applySync.ts` — чистый редьюсер
  (без импорта vue, тестируется без DOM), `staleness.ts`/`stream.ts` — давность потока и SSE, `store.ts` —
  сам стор, `instance.ts` — единственный экземпляр на приложение.
- `src/render/` — текст + `entities_json` -> VNode через `h()`; белый список обёрток
  `strong/em/code/pre/a/span`, безопасная проверка ссылок (`new URL` + белый список схем).
- `src/components/` — Vue-компоненты экрана.
- `src/styles/` — обычные `.css`-файлы, импортируются из `main.ts` (не инлайнятся, CSP `style-src 'self'`).
- `tests/` — vitest; каждый пункт спеки (7–12) — отдельный файл с номером в имени.
