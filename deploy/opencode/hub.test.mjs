// Регресс плагина хаба без модели и без OpenCode (Ф16-4):
//   node --experimental-strip-types deploy/opencode/hub.test.mjs
// Подменные spawn, часы, таймеры, журнал и клиент OpenCode.
import { EventEmitter } from "node:events"
import { HubPlugin } from "./hub.ts"

const { exitAction, parseWaitMark, parseWaitMarks, leaveArgv, RESTART_MIN_MS, WAIT_MARK } = HubPlugin._internals
// Сервер 4096 владельца держит пароль в окружении — детям плагина он уходить не должен (F19).
process.env.OPENCODE_SERVER_PASSWORD = "пароль-теста"

let fails = 0
let count = 0
function check(name, ok, detail = "") {
  count++
  if (!ok) fails++
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${!ok && detail ? " — " + detail : ""}`)
}
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)

// --- подмены ------------------------------------------------------------------------------------------------

function makeEnv() {
  const spawned = []
  const timers = []
  const logs = []
  const prompts = []
  let now = 1_000_000
  const deps = {
    now: () => now,
    log: (e) => logs.push(e),
    setTimeout: (fn, ms) => timers.push({ fn, at: now + ms }),
    spawn: (cmd, args, opts) => {
      const child = new EventEmitter()
      child.stdout = new EventEmitter()
      child.stderr = new EventEmitter()
      child.exitCode = null
      child.killed = false
      child.kill = (sig) => {
        child.killed = true
        child.signal = sig
      }
      child.cmd = cmd
      child.args = args
      child.opts = opts
      child.finish = (code, out = "", err = "") => {
        if (out) child.stdout.emit("data", Buffer.from(out))
        if (err) child.stderr.emit("data", Buffer.from(err))
        child.exitCode = code
        child.emit("close", code)
      }
      spawned.push(child)
      return child
    },
  }
  const ctx = {
    directory: "/fallback",
    client: {
      session: {
        get: async ({ path }) => ({ data: { id: path.id, directory: "/work/" + path.id } }),
        messages: async () => ({
          data: [
            { info: { role: "user", agent: "build", model: { providerID: "x", modelID: "y" } } },
            { info: { role: "assistant" } },
            {
              info: {
                role: "user",
                agent: "hub-project",
                model: { providerID: "deepseek", modelID: "deepseek-flash", variant: "max" },
              },
            },
            { info: { role: "assistant" } },
          ],
        }),
        promptAsync: async (req) => {
          prompts.push(req)
          return { response: { status: 204 } }
        },
      },
    },
  }
  return {
    deps,
    ctx,
    spawned,
    timers,
    logs,
    prompts,
    advance: (ms) => {
      now += ms
    },
    runTimers: () => {
      const due = timers.filter((t) => t.at <= now)
      for (const t of due) timers.splice(timers.indexOf(t), 1)
      for (const t of due) t.fn()
      return due.length
    },
  }
}

const tick = () => new Promise((r) => setTimeout(r, 0))
const ARGV = ["/opt/hub/.venv/bin/hub", "sub", "wait", "17", "--profile", "all"]
const mark = (argv = ARGV) => `Подписка #17 на проект 3.\n${WAIT_MARK}${JSON.stringify({ argv })}`
function subscribeEvent(sid, output, { callID = "call_1", status = "completed", tool = "hub-all_hub_subscribe" } = {}) {
  return {
    event: {
      type: "message.part.updated",
      properties: { sessionID: sid, part: { type: "tool", tool, sessionID: sid, callID, state: { status, output } } },
    },
  }
}

// --- чистые помощники -----------------------------------------------------------------------------------------

check("exit 0 -> доставить и перезапустить", exitAction(0) === "deliver_restart")
check("exit 6 -> перезапустить", exitAction(6) === "restart")
check("exit 2/3 -> доставить и стоп", exitAction(2) === "deliver_stop" && exitAction(3) === "deliver_stop")
check("exit 4/5/null/1 -> стоп молча", ["stop", "stop", "stop", "stop"].every((v, i) => exitAction([4, 5, null, 1][i]) === v))
check("метка: argv разобран", eq(parseWaitMark(mark()), ARGV))
check("метка: последняя строка с меткой", eq(parseWaitMark(`${WAIT_MARK}{"argv":["a","sub","wait","1","x"]}\n${mark()}`), ARGV))
check("метка: нет метки", parseWaitMark("просто текст") === null)
check("метка: битый JSON", parseWaitMark(`${WAIT_MARK}{argv`) === null)
check("метка: не sub wait", parseWaitMark(`${WAIT_MARK}${JSON.stringify({ argv: ["/bin/sh", "-c", "rm", "-rf", "/"] })}`) === null)
check("метка: уже с --oc-session", parseWaitMark(mark([...ARGV, "--oc-session", "ses_x"])) === null)
check("метка: не строка", parseWaitMark(undefined) === null)
check(
  "leave argv: профиль и адрес",
  eq(leaveArgv([...ARGV, "--url", "http://127.0.0.1:9/api/v1"], "ses_A", "/w"), [
    ARGV[0], "sub", "leave", "--oc-session", "ses_A", "--oc-dir", "/w", "--profile", "all", "--url", "http://127.0.0.1:9/api/v1",
  ]),
)

// --- подстановка аргументов ------------------------------------------------------------------------------------

{
  const env = makeEnv()
  const hooks = await HubPlugin(env.ctx, env.deps)
  const before = hooks["tool.execute.before"]
  // Ф34-2: напоминания (`hub_remind`, `hub_reminders`, `hub_remind_done`) тоже опознают сессию.
  // Ф35: реестр и сообщения агентов (`hub_agents`, `hub_agent_send`, `hub_agent_inbox`) — тоже.
  for (const tool of ["hub-all_hub_subscribe", "hub-all_hub_unsubscribe", "hub-all_hub_subscriptions", "hub-all_hub_react", "hub_hub_react", "hub-all_hub_join", "hub-all_hub_remind", "hub-all_hub_reminders", "hub-all_hub_remind_done", "hub-all_hub_agents", "hub-all_hub_agent_send", "hub-all_hub_agent_inbox", "hub-all_hub_draft", "hub_hub_draft", "hub-all_hub_handled", "hub-all_hub_mute", "hub-all_hub_propose_rule", "hub-all_hub_draft_new", "hub-all_hub_role", "hub-all_hub_request_session", "hub-all_hub_press", "hub-all_hub_presses", "hub-all_hub_human", "hub-all_hub_humans", "hub-all_hub_human_cancel"]) {
    const output = { args: { project_id: 3, oc_session: "ses_ЧУЖАЯ", oc_dir: "/tmp/чужой" } }
    await before({ tool, sessionID: "ses_A1234567", callID: "c" }, output)
    check(`подстановка: ${tool}`, output.args.oc_session === "ses_A1234567" && output.args.oc_dir === "/work/ses_A1234567" && output.args.project_id === 3)
  }
  // Ф26-1: прочие аргументы подписки (роль, режим, ключи) проходят как есть.
  {
    const output = { args: { role: "маркетолог", mode: "personal", keywords: ["штраф"], oc_session: "ses_ЧУЖАЯ" } }
    await before({ tool: "hub-all_hub_subscribe", sessionID: "ses_A1234567", callID: "c" }, output)
    check(
      "подстановка: role и ключи не режутся",
      output.args.role === "маркетолог" && output.args.mode === "personal" && eq(output.args.keywords, ["штраф"]) && output.args.oc_session === "ses_A1234567",
    )
  }
  // Ф37-1 П5: `hub_draft` опознаёт сессию (отправка без подтверждения — по роли её подписки); `oc_session`,
  // подставленный моделью, перезаписывается — чужой сессией представиться нельзя. Текст и адрес — как есть.
  {
    const output = { args: { channel_id: 283, text: "ответ", oc_session: "ses_ЧУЖАЯ", oc_dir: "/tmp/чужой" } }
    await before({ tool: "hub-all_hub_draft", sessionID: "ses_A1234567", callID: "c" }, output)
    check(
      "подстановка: hub_draft — oc_session модели перезаписан",
      eq(output.args, { channel_id: 283, text: "ответ", oc_session: "ses_A1234567", oc_dir: "/work/ses_A1234567" }),
    )
  }
  // Ф46-1 П9: новый параметр `hub_draft_new` — `from_message_id` (личка автору сообщения группы) — плагин
  // не срезает и не меняет; перезаписывается только опознание сессии.
  {
    const output = { args: { project_id: 3, account_id: 1, from_message_id: 55886, text: "привет", oc_session: "ses_ЧУЖАЯ" } }
    await before({ tool: "hub-all_hub_draft_new", sessionID: "ses_A1234567", callID: "c" }, output)
    check(
      "подстановка: hub_draft_new — from_message_id не срезан",
      eq(output.args, { project_id: 3, account_id: 1, from_message_id: 55886, text: "привет", oc_session: "ses_A1234567", oc_dir: "/work/ses_A1234567" }),
    )
  }
  // Ф39 B2: `hub_draft_new` теперь опознаёт сессию (автор черновика) — он в списке выше.
  for (const tool of ["hub-all_hub_draft_new_x", "hub-all_hub_drafts", "hub-all_hub_draft_edits", "hub-all_hub_handled_x", "hub-all_hub_mute_x", "hub-all_hub_muted", "hub-all_hub_wait", "bash", "read", "hub-all_hub_subscribe_x", "evil_hub_subscribe2", "hub-all_hub_joins", "hub-all_hub_reminder", "hub-all_hub_remind_x", "hub-all_hub_agent", "hub-all_hub_agent_sends", "hub-all_hub_agent_inbox_x", "hub-all_hub_rule_requests", "hub-all_hub_propose_rule_x", "hub-all_hub_agent_requests", "hub-all_hub_press_x", "hub-all_hub_pressed", "hub-all_hub_cards", "hub-all_hub_card_decide", "hub-all_hub_human_x", "hub-all_hub_humanoid", "hub-all_hub_human_cancelled"]) {
    const output = { args: { a: 1 } }
    await before({ tool, sessionID: "ses_A1234567", callID: "c" }, output)
    check(`без подстановки: ${tool}`, eq(output.args, { a: 1 }))
  }
  // Нет каталога от клиента — каталог плагина.
  const env2 = makeEnv()
  env2.ctx.client.session.get = async () => {
    throw new Error("нет")
  }
  const hooks2 = await HubPlugin(env2.ctx, env2.deps)
  const out2 = { args: {} }
  await hooks2["tool.execute.before"]({ tool: "hub-all_hub_react", sessionID: "ses_B1234567" }, out2)
  check("каталог — запасной ctx.directory", out2.args.oc_dir === "/fallback")
}

// --- ожидатель и коды выхода -------------------------------------------------------------------------------------

{
  const env = makeEnv()
  const hooks = await HubPlugin(env.ctx, env.deps)
  await hooks.event(subscribeEvent("ses_A", mark(), { status: "running" }))
  check("running — ожидатель не запущен", env.spawned.length === 0)
  await hooks.event(subscribeEvent("ses_A", "ответ без метки"))
  check("без метки — не запущен", env.spawned.length === 0)
  await hooks.event(subscribeEvent("ses_A", mark(), { tool: "hub-all_hub_subscriptions" }))
  check("не hub_subscribe — не запущен", env.spawned.length === 0)

  await hooks.event(subscribeEvent("ses_A", mark()))
  check("запущен один ожидатель", env.spawned.length === 1)
  const c1 = env.spawned[0]
  check("argv + сессия и каталог", c1.cmd === ARGV[0] && eq(c1.args, [...ARGV.slice(1), "--oc-session", "ses_A", "--oc-dir", "/work/ses_A"]))
  check("без shell, OPENCODE_PID в окружении", c1.opts.shell === undefined && !!c1.opts.env.OPENCODE_PID)
  check("пароль сервера не наследуется", !("OPENCODE_SERVER_PASSWORD" in c1.opts.env))

  await hooks.event(subscribeEvent("ses_A", mark()))
  check("тот же вызов повторно — не дублируется", env.spawned.length === 1)

  // Код 0: доставить stdout и перезапустить (не раньше 2 с от старта).
  env.advance(500)
  c1.finish(0, "Сигнал хаба по подписке #17 (проект 3): новое — карточек 1.\n")
  await tick()
  check("сигнал доставлен", env.prompts.length === 1 && env.prompts[0].body.parts[0].text.startsWith("Сигнал хаба"))
  const body = env.prompts[0].body
  check(
    "агент, модель и variant — из последней user-записи",
    env.prompts[0].path.id === "ses_A" && body.agent === "hub-project" && eq(body.model, { providerID: "deepseek", modelID: "deepseek-flash" }) && body.variant === "max",
  )
  check("перезапуск отложен до 2 с", env.spawned.length === 1 && env.timers.length === 1)
  env.advance(RESTART_MIN_MS)
  env.runTimers()
  check("перезапущен", env.spawned.length === 2)

  // Код 6: перезапуск без доставки; сразу после старта — через 2 с (защита от петли).
  const c2 = env.spawned[1]
  c2.finish(6, "непонятный сигнал")
  await tick()
  check("код 6 — без доставки", env.prompts.length === 1)
  check("код 6 — не сразу", env.spawned.length === 2 && env.timers.length === 1)
  env.advance(RESTART_MIN_MS - 1)
  env.runTimers()
  check("код 6 — ещё не перезапущен", env.spawned.length === 2)
  env.advance(1)
  env.runTimers()
  check("код 6 — перезапущен через 2 с", env.spawned.length === 3)

  // Код 3: доставить текст один раз и остановиться.
  const c3 = env.spawned[2]
  env.advance(60_000)
  c3.finish(3, "Хаб: подписка #17 снята — хаб переключён на другой движок.\n")
  await tick()
  env.advance(10_000)
  env.runTimers()
  check("код 3 — доставлен и стоп", env.prompts.length === 2 && env.spawned.length === 3 && env.timers.length === 0)
  check("журнал: выход с кодом 3 и стоп", env.logs.some((l) => l.ev === "wait_exit" && l.code === 3) && env.logs.some((l) => l.ev === "wait_stop"))
  check("журнал без текстов сигналов", !JSON.stringify(env.logs).includes("Сигнал хаба") && !JSON.stringify(env.logs).includes("переключён"))
}

{
  // Код 2 — текст из stderr, если stdout пуст; 4 и 5 — молча.
  for (const [code, delivered] of [[2, 1], [4, 0], [5, 0]]) {
    const env = makeEnv()
    const hooks = await HubPlugin(env.ctx, env.deps)
    await hooks.event(subscribeEvent("ses_A", mark()))
    env.advance(60_000)
    env.spawned[0].finish(code, "", "причина\n")
    await tick()
    env.advance(10_000)
    env.runTimers()
    check(`код ${code} — доставлено ${delivered}, без перезапуска`, env.prompts.length === delivered && env.spawned.length === 1)
  }
}

{
  // Новый ожидатель на ту же цель гасит прежний; выход прежнего ничего не делает. Другая цель — отдельно.
  const env = makeEnv()
  const hooks = await HubPlugin(env.ctx, env.deps)
  await hooks.event(subscribeEvent("ses_A", mark(), { callID: "c1" }))
  await hooks.event(subscribeEvent("ses_A", mark(), { callID: "c2" }))
  const [old, fresh] = env.spawned
  check("прежний погашен", old.killed && old.signal === "SIGTERM" && !fresh.killed)
  old.finish(4, "вытеснено")
  await tick()
  check("выход прежнего — без доставки и перезапуска", env.prompts.length === 0 && env.spawned.length === 2 && env.timers.length === 0)
  const chan = [...ARGV.slice(0, 3), "--channels", "--profile", "all"]
  await hooks.event(subscribeEvent("ses_A", mark(chan), { callID: "c3" }))
  await hooks.event(subscribeEvent("ses_B", mark(), { callID: "c4" }))
  check("другая цель и другая сессия — свои ожидатели", env.spawned.length === 4 && !fresh.killed)

  // session.deleted: гасит ожидатели сессии и снимает её подписки.
  await hooks.event({ event: { type: "session.deleted", properties: { sessionID: "ses_A", info: { id: "ses_A" } } } })
  check("ожидатели удалённой сессии погашены", fresh.killed && env.spawned[2].killed && !env.spawned[3].killed)
  const leave = env.spawned[4]
  check("hub sub leave запущен", !!leave && eq(leave.args, ["sub", "leave", "--oc-session", "ses_A", "--oc-dir", "/work/ses_A", "--profile", "all"]))
  fresh.finish(0, "Сигнал хаба")
  await tick()
  check("выход погашенного — без доставки", env.prompts.length === 0)
  await hooks.event({ event: { type: "session.deleted", properties: { sessionID: "ses_ZZZ", info: { id: "ses_ZZZ" } } } })
  check("незнакомая сессия — leave не зовётся", env.spawned.length === 5)
}

{
  // Упал spawn — журнал и стоп.
  const env = makeEnv()
  env.deps.spawn = () => {
    throw new Error("ENOENT")
  }
  const hooks = await HubPlugin(env.ctx, env.deps)
  await hooks.event(subscribeEvent("ses_A", mark()))
  check("spawn бросил — журнал, без перезапуска", env.logs.some((l) => l.ev === "spawn_failed") && env.timers.length === 0)
  const env2 = makeEnv()
  const hooks2 = await HubPlugin(env2.ctx, env2.deps)
  await hooks2.event(subscribeEvent("ses_A", mark()))
  env2.spawned[0].emit("error", new Error("ENOENT"))
  env2.spawned[0].emit("close", -2)
  await tick()
  check("ошибка процесса — стоп", env2.logs.some((l) => l.ev === "spawn_failed") && env2.timers.length === 0 && env2.spawned.length === 1)
}

{
  // Ф32-1 П5: подписка на проект с ролью вернула беседы — в ответе две метки, запускаются обе.
  const chan = [...ARGV.slice(0, 3), "--channels", "--profile", "all"]
  const line = (argv) => `${WAIT_MARK}${JSON.stringify({ argv })}`
  const both = `Подписка #17 на проект 3.\nВернулись подписки на беседы: #555 (c12).\n${line(chan)}\n${line(ARGV)}`
  check("метки: все по порядку", eq(parseWaitMarks(both), [chan, ARGV]))
  check("метки: нет меток и не строка", eq(parseWaitMarks("текст"), []) && eq(parseWaitMarks(undefined), []))
  check(
    "метки: негодная строка пропущена, годная осталась",
    eq(parseWaitMarks(`${WAIT_MARK}{argv\n${line(["/bin/sh", "-c", "rm", "-rf", "/"])}\n${line(ARGV)}`), [ARGV]),
  )
  const older = [...ARGV.slice(0, 3), "17", "--profile", "old"]
  check("метки: на одну цель — последняя", eq(parseWaitMarks(`${line(older)}\n${line(chan)}\n${line(ARGV)}`), [chan, ARGV]))

  const env = makeEnv()
  const hooks = await HubPlugin(env.ctx, env.deps)
  await hooks.event(subscribeEvent("ses_A", both))
  check("две метки — два ожидателя", env.spawned.length === 2)
  check(
    "ожидатели: беседы и проект, оба с сессией",
    eq(env.spawned[0].args, [...chan.slice(1), "--oc-session", "ses_A", "--oc-dir", "/work/ses_A"]) &&
      eq(env.spawned[1].args, [...ARGV.slice(1), "--oc-session", "ses_A", "--oc-dir", "/work/ses_A"]),
  )
  check("оба живы — друг друга не гасят", !env.spawned[0].killed && !env.spawned[1].killed)
  await hooks.event(subscribeEvent("ses_A", both))
  check("тот же вызов повторно — не дублируется", env.spawned.length === 2)
  await hooks.event({ event: { type: "session.deleted", properties: { sessionID: "ses_A", info: { id: "ses_A" } } } })
  check("удаление сессии гасит оба", env.spawned[0].killed && env.spawned[1].killed)
}

// --- Ф86-1 (C5): сводка дня при сжатии окна OpenCode вне хаба -------------------------------------------------

{
  const { digestArgv } = HubPlugin._internals
  check("digest argv", eq(digestArgv("/w"), ["digest", "show", "--cwd", "/w", "--max", "6000"]))
  delete process.env.HUB_OC_SERVER
  process.env.HUB_BIN = "/opt/hub/.venv/bin/hub"
  const env = makeEnv()
  const hooks = await HubPlugin(env.ctx, env.deps)
  const compacting = hooks["experimental.session.compacting"]
  check("хук сжатия есть", typeof compacting === "function")
  const output = { context: [] }
  const pending = compacting({ sessionID: "ses_D" }, output)
  await tick()
  const child = env.spawned.at(-1)
  check(
    "hub digest show по папке сессии",
    !!child && child.cmd === "/opt/hub/.venv/bin/hub" && eq(child.args, ["digest", "show", "--cwd", "/work/ses_D", "--max", "6000"]),
  )
  check("пароль сервера не уходит в hub digest", !!child && child.opts.env.OPENCODE_SERVER_PASSWORD === undefined)
  child.finish(0, '<hub_digest at="10:00" project="P">\nсводка\n</hub_digest>\n')
  await pending
  check("сводка в output.context", eq(output.context, ['<hub_digest at="10:00" project="P">\nсводка\n</hub_digest>']))

  const empty = { context: [] }
  const p2 = compacting({ sessionID: "ses_D" }, empty)
  await tick()
  env.spawned.at(-1).finish(0, "")
  await p2
  check("сводки нет — context пуст", eq(empty.context, []))

  const failed = { context: [] }
  const p3 = compacting({ sessionID: "ses_D" }, failed)
  await tick()
  env.spawned.at(-1).finish(1, "что-то")
  await p3
  check("код не 0 — context пуст", eq(failed.context, []))

  const slow = { context: [] }
  const p4 = compacting({ sessionID: "ses_D" }, slow)
  await tick()
  const hung = env.spawned.at(-1)
  env.advance(6000)
  env.runTimers()
  await p4
  check("дольше 5 с — пусто и процесс погашен", eq(slow.context, []) && hung.killed)

  process.env.HUB_OC_SERVER = "1"
  const spawnedBefore = env.spawned.length
  const own = { context: [] }
  await compacting({ sessionID: "ses_D" }, own)
  check("сервер OpenCode хаба — молчит", eq(own.context, []) && env.spawned.length === spawnedBefore)
  delete process.env.HUB_OC_SERVER
  delete process.env.HUB_BIN
}

console.log(`checks=${count} fails=${fails}`)
process.exit(fails ? 1 : 0)
