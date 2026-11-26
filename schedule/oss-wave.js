export const meta = {
  name: 'oss-wave',
  description: 'Run scoped factory roles with independent value and code review',
  whenToUse: 'Pass factory, concurrency and tasks with key, role, item, repo, optional lock_key and note',
  phases: [{ title: 'Prepare' }, { title: 'Review' }, { title: 'Fix' }],
}

const A = args || {}
const F = A.factory === undefined ? '$HOME/Documents/software-factory' : A.factory
const MAX = A.concurrency === undefined ? 4 : A.concurrency
const roles = ['scout', 'value-review', 'implement', 'review', 'fix', 'follow', 'coordinator-review', 'takeover', 'record']
const aliases = { hunt: 'scout', finish: 'implement' }
if (typeof F !== 'string' || !F) throw new Error('args.factory must name the factory directory')
if (!Number.isInteger(MAX) || MAX < 1) throw new Error('args.concurrency must be a positive integer')
if (!Array.isArray(A.tasks) || !A.tasks.length) throw new Error('args.tasks must be a non-empty array')

const keys = new Set()
const tasks = A.tasks.map((task, index) => {
  const kind = task.role || task.kind
  const role = aliases[kind] || kind
  if (!roles.includes(role)) throw new Error(`Unsupported task role: ${kind}`)
  const key = task.key || `${kind}-${index + 1}`
  if (keys.has(key)) throw new Error(`Duplicate task key: ${key}`)
  keys.add(key)
  for (const field of ['item', 'repo', 'lock_key', 'note', 'prompt']) {
    if (task[field] !== undefined && typeof task[field] !== 'string') throw new Error(`Task ${key}: ${field} must be a string`)
  }
  const lockKey = (task.lock_key === undefined ? task.repo || '' : task.lock_key).split('/').pop().toLowerCase()
  return { key, role, item: task.item, repo: task.repo, lock_key: lockKey, note: task.note === undefined ? task.prompt : task.note }
})

const resultSchema = role => ({
  type: 'object',
  properties: {
    item: { type: 'string' },
    head: { type: 'string' },
    verdict: { type: 'string', enum: role === 'scout' || role === 'value-review' ? ['build', 'propose', 'drop'] : ['ship', 'fix-needed', 'drop'] },
    summary: { type: 'string' },
    ...(role.includes('review') ? { complete: { type: 'boolean' } } : {}),
  },
  required: role.includes('review') ? ['item', 'head', 'summary', 'complete'] : ['item', 'head', 'verdict', 'summary'],
  additionalProperties: false,
})
const quote = value => "'" + String(value).replace(/'/g, "'\\''") + "'"
const factoryPrefix = F.replace(/\/+$/, '') + '/'
const defaultTail = '/Documents/software-factory/'
const normalizeItem = item => {
  if (!item) return item
  if (item.startsWith(factoryPrefix)) {
    item = item.slice(factoryPrefix.length)
  } else if (A.factory === undefined && /^(\/|~|\$HOME)/.test(item) && item.includes(defaultTail)) {
    // Without an explicit factory the script cannot expand $HOME, so the default checkout is recognised by its tail.
    item = item.slice(item.indexOf(defaultTail) + defaultTail.length)
  }
  return item.replace(/\/+$/, '')
}
// Items are compared in their normalised form, but a worker may run the command from any directory.
const itemArgument = item => {
  if (/^(\/|~|\$HOME)/.test(item)) return quote(item)
  return A.factory === undefined ? `"${factoryPrefix}"${quote(item)}` : quote(`${factoryPrefix}${item}`)
}
const promptFor = (task, role, item) => {
  const binary = A.factory === undefined ? `"${factoryPrefix}bin/factory"` : quote(`${factoryPrefix}bin/factory`)
  const command = [binary, 'brief', quote(role)]
  if (item) command.push('--item', itemArgument(item))
  if (task.repo) command.push('--repo', quote(task.repo))
  if (task.note) command.push('--task', quote(task.note))
  const fields = role.includes('review') ? 'item, head, verdict, summary, complete' : 'item, head, verdict, summary'
  const files = "The item's validation.md, verify.md, interview.md, pr.md and proposal.md are deliverables: " +
    'the coordinator asks you to edit the existing files in place. Create no other Markdown reports in the item; ' +
    'Markdown the upstream project requires in the change itself, such as a .changeset entry, is part of the code.'
  const evidence = role === 'scout' ? ' Keep raw API dumps and tree listings out of tracked paths: use a local scratch directory or the ignored runs/<date>/<shift>/artifacts/ directory. Record only the evidence you cite, with source URLs and concise excerpts, in curated item or shift records outside artifacts/.' : ''
  return `Run this command, follow the brief it prints, then return only {${fields}}. ${files}${evidence}\n${command.join(' ')}`
}

let active = 0
const waiters = []
async function slot(fn) {
  while (active >= MAX) await new Promise(resolve => waiters.push(resolve))
  active++
  try { return await fn() } finally {
    active--
    const next = waiters.shift()
    if (next) next()
  }
}

async function run(task, role, item, stage) {
  item = normalizeItem(item)
  const schema = resultSchema(role)
  let phaseName = 'Prepare'
  if (role.includes('review')) {
    phaseName = 'Review'
  } else if (['fix', 'follow', 'takeover'].includes(role)) {
    phaseName = 'Fix'
  }
  for (let attempt = 1; attempt <= 4; attempt++) {
    const label = `${task.key}:${stage}:${role}:attempt-${attempt}/4`
    const prompt = promptFor(task, role, item) + (attempt > 1 ? `\nAttempt ${attempt}/4: the previous attempt returned nothing; reuse its item artifacts.` : '')
    const opts = { label, phase: phaseName, schema }
    if (A.model) opts.model = A.model
    let result = await slot(() => agent(prompt, opts))
    if (result === null || result === undefined || result === '') {
      log(`${label}: returned nothing`)
      continue
    }
    if (typeof result !== 'object' || ['item', 'head', 'summary'].some(field => typeof result[field] !== 'string') ||
        (result.complete !== undefined && typeof result.complete !== 'boolean')) {
      throw new Error(`${label}: invalid result; correct the input or schema before retrying`)
    }
    result = { ...result, item: normalizeItem(result.item) }
    if (item && result.item !== item) throw new Error(`${label}: returned a different item`)
    if (role.includes('review') && (result.complete !== true || !result.verdict)) {
      return { ...result, complete: false, verdict: 'incomplete', unfinished: true }
    }
    if (!schema.properties.verdict.enum.includes(result.verdict)) throw new Error(`${label}: invalid verdict; correct the result before retrying`)
    if (['build', 'propose'].includes(result.verdict) && !result.item) throw new Error(`${label}: ${result.verdict} requires an item; return the saved item path`)
    return result
  }
  let verdict = 'fix-needed'
  if (role.includes('review')) {
    verdict = 'incomplete'
  } else if (role === 'scout') {
    verdict = 'drop'
  }
  return {
    item: item || '',
    head: '',
    verdict,
    summary: `${task.key}: ${role} returned nothing after 4 attempts; inspect partial artifacts`,
    unfinished: true,
  }
}

async function execute(task) {
  let role = task.role
  let result = await run(task, role, task.item, 1)
  let stage = 2
  if (result.unfinished) return result
  if (role === 'scout') {
    if (!['build', 'propose'].includes(result.verdict)) return result
    result = await run(task, 'value-review', result.item, stage++)
    role = 'value-review'
    if (result.unfinished) return result
  }
  if (role === 'value-review') {
    if (result.verdict !== 'build') return result
    result = await run(task, 'implement', result.item, stage++)
    role = 'implement'
    if (result.unfinished) return result
  }
  if (['implement', 'fix', 'follow', 'takeover'].includes(role)) {
    if (result.verdict !== 'ship' || !result.item) return result
    result = await run(task, 'review', result.item, stage++)
    if (result.unfinished) return result
  }
  if (['implement', 'review'].includes(role) && result.complete === true && result.verdict === 'fix-needed' && result.item) {
    result = await run(task, 'fix', result.item, stage++)
    if (result.verdict === 'ship') result = await run(task, 'review', result.item, stage++)
  }
  return result
}

// Missing repository identity serializes the wave, including legacy notes-only tasks.
const serializeAll = tasks.some(task => !task.lock_key)
const queues = new Map()
async function inRepo(task) {
  const key = serializeAll ? '*' : task.lock_key
  const previous = queues.get(key) || Promise.resolve()
  let release
  const current = new Promise(resolve => { release = resolve })
  queues.set(key, current)
  await previous
  try {
    const { item, head, verdict, summary } = await execute(task)
    return { item, head, verdict, summary }
  } finally { release() }
}

return await parallel(tasks.map(task => () => inRepo(task)))
