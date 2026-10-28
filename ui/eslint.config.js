// Flat config (ESLint 9). Правило проекта (spec 06, «Dev-сервера...», «`v-html` запрещён»): рендер
// сообщений — только через text-узлы/белый список, поэтому здесь же дублируем список опасных API тестом-
// сторожем (`tests/11-guard.spec.ts`) — эти правила ловят их на этапе `npm run lint`, тест сторожит исходники
// напрямую (на случай, если кто-то отключит eslint-комментарием).
import js from '@eslint/js';
import vue from 'eslint-plugin-vue';
import tseslint from 'typescript-eslint';

// Ручной список глобалов вместо пакета `globals`, чтобы не тянуть лишнюю зависимость сверх списка спеки.
const browserGlobals = {
  window: 'readonly',
  document: 'readonly',
  navigator: 'readonly',
  location: 'readonly',
  history: 'readonly',
  fetch: 'readonly',
  console: 'readonly',
  crypto: 'readonly',
  URL: 'readonly',
  URLSearchParams: 'readonly',
  EventSource: 'readonly',
  Notification: 'readonly',
  IntersectionObserver: 'readonly',
  MutationObserver: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly',
  requestAnimationFrame: 'readonly',
  structuredClone: 'readonly',
  localStorage: 'readonly',
  sessionStorage: 'readonly',
};

const nodeGlobals = {
  process: 'readonly',
  __dirname: 'readonly',
};

const dangerousProperties = [
  { property: 'innerHTML', message: 'innerHTML запрещён — рендер только через text-узлы (см. src/render).' },
  { property: 'outerHTML', message: 'outerHTML запрещён — рендер только через text-узлы (см. src/render).' },
  {
    property: 'insertAdjacentHTML',
    message: 'insertAdjacentHTML запрещён — рендер только через text-узлы (см. src/render).',
  },
  { object: 'document', property: 'write', message: 'document.write запрещён.' },
  { object: 'localStorage', message: 'localStorage запрещён в ui/src (правило проекта).' },
  { object: 'sessionStorage', message: 'sessionStorage запрещён в ui/src (правило проекта).' },
];

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...vue.configs['flat/recommended'],
  {
    languageOptions: {
      globals: { ...browserGlobals, ...nodeGlobals },
    },
  },
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
      },
    },
  },
  {
    rules: {
      'vue/multi-word-component-names': 'off',
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
  {
    // Опасные API запрещены в ПРИЛОЖЕНИИ (`src/`) — не в тестах: тест 10 законно читает
    // localStorage/sessionStorage/document.cookie, чтобы ДОКАЗАТЬ, что приложение их не использует.
    files: ['src/**'],
    rules: {
      'vue/no-v-html': 'error',
      'no-restricted-properties': ['error', ...dangerousProperties],
      'no-restricted-syntax': [
        'error',
        { selector: "CallExpression[callee.name='eval']", message: 'eval запрещён.' },
        { selector: "NewExpression[callee.name='Function']", message: 'new Function запрещён.' },
        {
          selector: "MemberExpression[property.name='cookie'][object.name='document']",
          message: 'document.cookie запрещён в ui/src (правило проекта): cookie — только HttpOnly на сервере.',
        },
      ],
    },
  },
);
