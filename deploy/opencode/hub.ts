// Плагин хаба для OpenCode владельца (Ф16-4, docs/architecture.md).
// Ставит его scripts/opencode-hub-install.sh симлинком ~/.config/opencode/plugins/hub.ts -> дерево hub-prod
// (деплой обновляет плагин сам). Регресс без модели:
//   node --experimental-strip-types deploy/opencode/hub.test.mjs
//
// Что делает:
// 1. Опознание сессии. У OpenCode нет реестра «pid -> сессия», как у Claude Code, а один процесс (сервер
//    4096) держит много сессий. В `tool.execute.before` для инструментов хаба, которые опознают сессию
//    (`*_hub_subscribe|unsubscribe|subscriptions|react|join|remind|reminders|remind_done`, Ф35:
//    `*_hub_agents|agent_send|agent_inbox`, Ф37-1: `*_hub_draft`, Ф37-3: `*_hub_handled`, Ф38-3:
//    `*_hub_propose_rule`, Ф39: `*_hub_draft_new`), плагин
//    ПЕРЕЗАПИСЫВАЕТ аргументы `oc_session`
//    (id сессии) и `oc_dir` (её каталог) — модель их не выбирает.
// 2. Ожидание сигналов. У OpenCode нет фоновых команд, которые будят сессию. Ответ `hub_subscribe` для
//    OpenCode кончается строкой `HUB-OC-WAIT {"argv": [...]}` (Ф32-1: таких строк может быть несколько —
//    запускаются все); плагин ловит её в `message.part.updated` и запускает `hub sub wait … --oc-session S --oc-dir D` (без shell). По коду выхода: 0 — доставить
//    stdout в сессию и перезапустить; 6 — перезапустить; 3 и 2 — доставить текст один раз и остановиться;
//    4 и 5 — остановиться молча. Перезапуск — не чаще раза в 2 с. Один ожидатель на (сессия, цель); новый
//    гасит прежний.
// 3. Доставка — `session.promptAsync` с агентом, моделью и variant последней `user`-записи сессии (сигнал
//    не переключает агента/модель). Занята сессия — всё равно (встанет в очередь хода).
// 4. `session.deleted` — погасить ожидатели сессии и снять её подписки (`hub sub leave`; неудача не страшна).
// 5. Ф86-1 (C5, П8): `experimental.session.compacting` — перед сжатием разговора в окне OpenCode вне хаба
//    сводка дня проекта папки сессии (`hub digest show --cwd <папка> --max 6000`, без shell, до 5 с) идёт в
//    `output.context`. В сервере OpenCode хаба (`HUB_OC_SERVER=1` в окружении) молчит: хаб подкладывает сам.
// Журнал — ~/.local/state/hub/opencode-plugin.log (или $HUB_OC_PLUGIN_LOG), до 1 МиБ (дальше — .1):
// только события, номера, коды и ошибки — без текстов сообщений и секретов.
//
// Экспорт — ОДИН (OpenCode зовёт каждую экспортированную функцию как плагин); чистые помощники для теста —
// свойство `HubPlugin._internals`, второй аргумент фабрики — подмена зависимостей (spawn, часы, журнал).

import { spawn as nodeSpawn } from "node:child_process"
import { appendFileSync, mkdirSync, renameSync, statSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

// Ф17-1 B: `hub_join` — опознанная сессия нужна выключателю проекта (иначе заявка просто идёт через владельца).
// Ф34-2 П2: напоминания ставит и ведёт подписанная сессия — `hub_remind`, `hub_reminders`, `hub_remind_done`.
// Ф35-1b, Ф35-2b: реестр и сообщения агентов — `hub_agents`, `hub_agent_send`, `hub_agent_inbox`.
// Ф37-1 B1 + П5: `hub_draft` — по сессии ядро решает, уходит ли черновик без подтверждения владельца (роль её
// подписки); якорь `$` не пускает сюда `hub_drafts`, `hub_draft_edits`.
// Ф39 B2: `hub_draft_new` — по сессии ядро записывает автора черновика (роль её подписки).
// Ф37-3 D1: `hub_handled` — отметку «разобрано» ставит подписанная сессия проекта с ролью.
// Ф38-3 A1: `hub_propose_rule` — по сессии ядро подписывает заявку на правило раскладки ролью её подписки.
// Ф41-8: `hub_role` — правила роли подписки этой сессии.
// Ф41-5б: `hub_request_session` — заявку на напарника подаёт сессия с подпиской на проект с ролью.
// Ф44-1c: `hub_press`, `hub_presses` — нажатие кнопки бота и свои нажатия: ядро сверяет роль сессии и
// отдаёт ответ бота только автору.
// Ф50-4: `hub_agent_state` — состояние роли отдаётся только сессии с живой подпиской на её проект.
// Ф54-1: `hub_human`, `hub_humans`, `hub_human_cancel` — заявку «нужен человек» кладёт, видит и снимает
// сессия с подпиской на проект с ролью (ядро считает пределы и право отмены по линии её роли).
// Ф56-1б: `hub_ask_owner` — вопрос владельцу задаёт сессия с подпиской-ролью.
// Ф63-1: `hub_inbound_rules` — правила входящих своего проекта заводит сессия с подпиской-ролью.
// Ф62-6: `hub_group_create`, `hub_group_members`, `hub_group_link`, `hub_group_ops` — исход заявки по группе
// WhatsApp ядро шлёт сигналом подписке автора и отдаёт списком только её заявки.
// Ф86-1: `hub_digest` — сводка дня проекта подписки этой сессии.
// Ф99-2: `hub_mute` — заглушку ставит и снимает подписанная сессия с ролью (ядро помнит её подписку).
const SESSION_TOOL_NAMES = [
  "subscribe", "unsubscribe", "subscriptions", "react", "join", "remind", "reminders", "remind_done", "agents",
  "agent_send", "agent_inbox", "agent_state", "ask_owner", "draft", "draft_new", "handled", "mute",
  "propose_rule", "role", "request_session", "press", "presses", "human", "humans", "human_cancel",
  "triage_mark", "inbound_rules", "group_create", "group_members", "group_link", "group_ops",
  "digest",
]
const SESSION_TOOLS = new RegExp(`_hub_(${SESSION_TOOL_NAMES.join("|")})$`)
const SUBSCRIBE_TOOL = /_hub_subscribe$/
const DIGEST_MAX = 6000
const DIGEST_TIMEOUT_MS = 5000
const WAIT_MARK = "HUB-OC-WAIT "
const RESTART_MIN_MS = 2000
const LOG_MAX = 1024 * 1024
const OUT_MAX = 64 * 1024

type Action = "deliver_restart" | "restart" | "deliver_stop" | "stop"

// Код выхода `hub sub wait` -> что делать (3.5).
function exitAction(code: number | null): Action {
  if (code === 0) return "deliver_restart"
  if (code === 6) return "restart"
  if (code === 2 || code === 3) return "deliver_stop"
  return "stop" // 4 — вытеснено, 5 — сессии нет, прочее (сигнал, упал) — журнал и стоп
}

// Одна строка с меткой -> команда ожидания: JSON `{"argv": [строки]}`; негодная — `null`.
function parseMarkLine(line: string): string[] | null {
  try {
    const data = JSON.parse(line.slice(WAIT_MARK.length))
    const argv = data?.argv
    if (!Array.isArray(argv) || argv.length < 5 || !argv.every((a) => typeof a === "string" && a.length)) return null
    if (argv[1] !== "sub" || argv[2] !== "wait") return null
    if (argv.includes("--oc-session") || argv.includes("--oc-dir")) return null
    return argv
  } catch {
    return null
  }
}

// Команда ожидания из ответа `hub_subscribe`: последняя строка с меткой.
function parseWaitMark(output: unknown): string[] | null {
  if (typeof output !== "string") return null
  const lines = output.split("\n").filter((l) => l.startsWith(WAIT_MARK))
  if (!lines.length) return null
  return parseMarkLine(lines[lines.length - 1])
}

// Ф32-1 П5: ВСЕ команды ожидания ответа по порядку (подписка на проект с ролью возвращает и беседы — в
// ответе две метки: `--channels` и подписка на проект). На одну цель (argv[3]) — последняя метка; негодные
// строки пропускаются.
function parseWaitMarks(output: unknown): string[][] {
  if (typeof output !== "string") return []
  const byTarget = new Map<string, string[]>()
  for (const line of output.split("\n")) {
    if (!line.startsWith(WAIT_MARK)) continue
    const argv = parseMarkLine(line)
    if (!argv) continue
    byTarget.delete(argv[3])
    byTarget.set(argv[3], argv)
  }
  return [...byTarget.values()]
}

// `hub sub leave` из команды ожидания: тот же бинарь, профиль и адрес ядра.
function leaveArgv(waitArgv: string[], sessionID: string, dir: string): string[] {
  const out = [waitArgv[0], "sub", "leave", "--oc-session", sessionID, "--oc-dir", dir]
  for (const flag of ["--profile", "--url"]) {
    const i = waitArgv.indexOf(flag)
    if (i > 0 && i + 1 < waitArgv.length) out.push(flag, waitArgv[i + 1])
  }
  return out
}

// Ф86-1: бинарь хаба для `hub digest show` — $HUB_BIN, иначе тот, что в последней команде ожидания, иначе
// боевое дерево (~/projects/hub-prod).
function hubBinary(known: string | undefined): string {
  return process.env.HUB_BIN || known || join(homedir(), "projects", "hub-prod", ".venv", "bin", "hub")
}

function digestArgv(dir: string): string[] {
  return ["digest", "show", "--cwd", dir, "--max", String(DIGEST_MAX)]
}

function defaultLogPath(): string {
  return process.env.HUB_OC_PLUGIN_LOG || join(homedir(), ".local", "state", "hub", "opencode-plugin.log")
}

function fileLog(path: string) {
  return (entry: Record<string, unknown>) => {
    try {
      mkdirSync(dirname(path), { recursive: true })
      try {
        if (statSync(path).size > LOG_MAX) renameSync(path, path + ".1")
      } catch {}
      appendFileSync(path, JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n")
    } catch {}
  }
}

type Deps = {
  spawn: typeof nodeSpawn
  now: () => number
  log: (entry: Record<string, unknown>) => void
  setTimeout: (fn: () => void, ms: number) => unknown
}

type Waiter = {
  key: string
  sessionID: string
  dir: string
  argv: string[]
  child: any
  lastStart: number
  stopped: boolean
}

export const HubPlugin = async (ctx: any, deps?: Partial<Deps>) => {
  const d: Deps = {
    spawn: deps?.spawn ?? nodeSpawn,
    now: deps?.now ?? Date.now,
    log: deps?.log ?? fileLog(defaultLogPath()),
    setTimeout: deps?.setTimeout ?? ((fn, ms) => setTimeout(fn, ms)),
  }
  const dirs = new Map<string, string>() // сессия -> каталог
  const waiters = new Map<string, Waiter>() // "сессия|цель" -> ожидатель
  const lastArgv = new Map<string, string[]>() // сессия -> последняя команда ожидания (для leave)
  const seenCalls = new Set<string>()

  async function sessionDir(sessionID: string): Promise<string> {
    const cached = dirs.get(sessionID)
    if (cached) return cached
    let dir = ""
    try {
      const res = await ctx?.client?.session?.get?.({ path: { id: sessionID } })
      dir = res?.data?.directory || ""
    } catch (e) {
      d.log({ ev: "session_get_failed", sid: sessionID, err: String(e).slice(0, 200) })
    }
    if (!dir) dir = ctx?.directory || process.cwd()
    dirs.set(sessionID, dir)
    return dir
  }

  function childEnv(): Record<string, string | undefined> {
    const env: Record<string, string | undefined> = { ...process.env }
    delete env.OPENCODE_SERVER_PASSWORD // пароль сервера детям не нужен (F19)
    if (!env.OPENCODE_PID) env.OPENCODE_PID = String(process.pid)
    return env
  }

  async function deliver(sessionID: string, text: string): Promise<void> {
    const body: Record<string, unknown> = { parts: [{ type: "text", text }] }
    try {
      const res = await ctx.client.session.messages({ path: { id: sessionID } })
      const list = Array.isArray(res?.data) ? res.data : []
      for (let i = list.length - 1; i >= 0; i--) {
        const info = list[i]?.info
        if (info?.role !== "user") continue
        if (info.agent) body.agent = info.agent
        if (info.model?.providerID && info.model?.modelID) {
          body.model = { providerID: info.model.providerID, modelID: info.model.modelID }
          if (info.model.variant) body.variant = info.model.variant
        }
        if (info.variant && !body.variant) body.variant = info.variant
        break
      }
    } catch (e) {
      d.log({ ev: "messages_failed", sid: sessionID, err: String(e).slice(0, 200) })
    }
    try {
      await ctx.client.session.promptAsync({ path: { id: sessionID }, body })
      d.log({ ev: "delivered", sid: sessionID, chars: text.length })
    } catch (e) {
      d.log({ ev: "deliver_failed", sid: sessionID, err: String(e).slice(0, 200) })
    }
  }

  function run(w: Waiter): void {
    if (w.stopped) return
    const args = [...w.argv.slice(1), "--oc-session", w.sessionID, "--oc-dir", w.dir]
    let child: any
    try {
      child = d.spawn(w.argv[0], args, { stdio: ["ignore", "pipe", "pipe"], env: childEnv() })
    } catch (e) {
      d.log({ ev: "spawn_failed", key: w.key, err: String(e).slice(0, 200) })
      stop(w)
      return
    }
    w.child = child
    w.lastStart = d.now()
    d.log({ ev: "wait_start", key: w.key })
    let out = ""
    let err = ""
    child.stdout?.on?.("data", (chunk: any) => {
      if (out.length < OUT_MAX) out += String(chunk)
    })
    child.stderr?.on?.("data", (chunk: any) => {
      if (err.length < OUT_MAX) err += String(chunk)
    })
    let done = false
    child.on("error", (e: any) => {
      if (done) return
      done = true
      d.log({ ev: "spawn_failed", key: w.key, err: String(e?.message ?? e).slice(0, 200) })
      if (w.child === child) stop(w)
    })
    child.on("close", (code: number | null) => {
      if (done) return
      done = true
      if (w.child !== child || w.stopped) return // заменён новым ожидателем или погашен
      const action = exitAction(code)
      d.log({ ev: "wait_exit", key: w.key, code, action })
      const text = (out.trim() || err.trim()).slice(0, OUT_MAX)
      if ((action === "deliver_restart" || action === "deliver_stop") && text) void deliver(w.sessionID, text)
      if (action === "deliver_restart" || action === "restart") {
        const wait = Math.max(0, w.lastStart + RESTART_MIN_MS - d.now())
        if (wait > 0) d.setTimeout(() => run(w), wait)
        else run(w)
      } else {
        stop(w)
      }
    })
  }

  function stop(w: Waiter): void {
    w.stopped = true
    if (waiters.get(w.key) === w) waiters.delete(w.key)
    const child = w.child
    if (child && child.exitCode == null && !child.killed) {
      try {
        child.kill("SIGTERM")
      } catch {}
    }
    d.log({ ev: "wait_stop", key: w.key })
  }

  function startWaiter(sessionID: string, dir: string, argv: string[]): void {
    const key = `${sessionID}|${argv[3]}`
    const prev = waiters.get(key)
    if (prev) stop(prev)
    const w: Waiter = { key, sessionID, dir, argv, child: null, lastStart: 0, stopped: false }
    waiters.set(key, w)
    lastArgv.set(sessionID, argv)
    run(w)
  }

  function sessionDeleted(sessionID: string): void {
    for (const w of [...waiters.values()]) if (w.sessionID === sessionID) stop(w)
    const argv = lastArgv.get(sessionID)
    const dir = dirs.get(sessionID)
    lastArgv.delete(sessionID)
    dirs.delete(sessionID)
    if (!argv || !dir) return
    const leave = leaveArgv(argv, sessionID, dir)
    try {
      const child = d.spawn(leave[0], leave.slice(1), { stdio: "ignore", env: childEnv() })
      child.on?.("error", (e: any) => d.log({ ev: "leave_failed", sid: sessionID, err: String(e?.message ?? e).slice(0, 200) }))
      child.on?.("close", (code: number | null) => d.log({ ev: "leave_exit", sid: sessionID, code }))
    } catch (e) {
      d.log({ ev: "leave_failed", sid: sessionID, err: String(e).slice(0, 200) })
    }
  }

  // Ф86-1 (C5): сводка дня проекта папки — stdout `hub digest show` (пусто — сводки нет). Ошибка, код не 0
  // или дольше 5 с — пусто.
  function digestFor(dir: string): Promise<string> {
    const known = [...lastArgv.values()].at(-1)?.[0]
    return new Promise((resolve) => {
      let child: any
      try {
        child = d.spawn(hubBinary(known), digestArgv(dir), { stdio: ["ignore", "pipe", "ignore"], env: childEnv() })
      } catch (e) {
        d.log({ ev: "digest_failed", err: String(e).slice(0, 200) })
        resolve("")
        return
      }
      let out = ""
      let done = false
      const finish = (text: string) => {
        if (done) return
        done = true
        resolve(text)
      }
      child.stdout?.on?.("data", (chunk: any) => {
        if (out.length < OUT_MAX) out += String(chunk)
      })
      child.on("error", (e: any) => {
        d.log({ ev: "digest_failed", err: String(e?.message ?? e).slice(0, 200) })
        finish("")
      })
      child.on("close", (code: number | null) => {
        d.log({ ev: "digest_exit", code, chars: out.trim().length })
        finish(code === 0 ? out.trim().slice(0, OUT_MAX) : "")
      })
      d.setTimeout(() => {
        if (done) return
        d.log({ ev: "digest_timeout" })
        try {
          child.kill("SIGTERM")
        } catch {}
        finish("")
      }, DIGEST_TIMEOUT_MS)
    })
  }

  d.log({ ev: "loaded", pid: process.pid })

  return {
    "tool.execute.before": async (input: any, output: any) => {
      if (!SESSION_TOOLS.test(String(input?.tool ?? ""))) return
      const sessionID = String(input?.sessionID ?? "")
      if (!sessionID) return
      if (!output.args || typeof output.args !== "object") output.args = {}
      output.args.oc_session = sessionID
      output.args.oc_dir = await sessionDir(sessionID)
    },
    "experimental.session.compacting": async (input: any, output: any) => {
      if (process.env.HUB_OC_SERVER === "1") return // П8: сервер OpenCode хаба — сводку подаёт хаб
      const sessionID = String(input?.sessionID ?? "")
      if (!sessionID || !output || typeof output !== "object") return
      const text = await digestFor(await sessionDir(sessionID))
      if (!text) return
      if (!Array.isArray(output.context)) output.context = []
      output.context.push(text)
      d.log({ ev: "digest_context", sid: sessionID, chars: text.length })
    },
    event: async ({ event }: any) => {
      const type = event?.type
      const props = event?.properties ?? {}
      if (type === "message.part.updated") {
        const part = props.part
        if (!part || part.type !== "tool" || !SUBSCRIBE_TOOL.test(String(part.tool ?? ""))) return
        if (part.state?.status !== "completed") return
        const marks = parseWaitMarks(part.state?.output)
        if (!marks.length) return
        const callKey = `${part.sessionID}|${part.callID}`
        if (seenCalls.has(callKey)) return
        seenCalls.add(callKey)
        if (seenCalls.size > 1000) seenCalls.delete(seenCalls.values().next().value as string)
        const sessionID = String(part.sessionID ?? props.sessionID ?? "")
        if (!sessionID) return
        const dir = await sessionDir(sessionID)
        for (const argv of marks) startWaiter(sessionID, dir, argv)
      } else if (type === "session.deleted") {
        const sessionID = String(props.sessionID ?? props.info?.id ?? "")
        if (sessionID) sessionDeleted(sessionID)
      }
    },
  }
}

HubPlugin._internals = {
  exitAction,
  parseWaitMark,
  parseWaitMarks,
  leaveArgv,
  digestArgv,
  RESTART_MIN_MS,
  WAIT_MARK,
}
