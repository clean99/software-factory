export const meta = {
  name: 'eval-run',
  description: 'Run frozen reading cases and return answer paths and attempt counts',
  whenToUse: 'Regression checks after changing a role card or execution route',
  phases: [{ title: 'Evaluate' }],
}

const A = args || {}
if (typeof A.factory !== 'string' || !A.factory.startsWith('/') ||
    typeof A.run_dir !== 'string' || !A.run_dir.startsWith('/') ||
    !Array.isArray(A.cases) || !A.cases.length ||
    A.cases.some(id => typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(id)) ||
    new Set(A.cases).size !== A.cases.length) {
  throw new Error('Supply absolute factory and run_dir paths and a nonempty list of unique case ids.')
}
const MAX = A.concurrency ?? 4
if (!Number.isInteger(MAX) || MAX < 1 || MAX > 4) {
  throw new Error('concurrency must be an integer from 1 to 4.')
}
const quote = value => `'${value.replace(/'/g, `'"'"'`)}'`
const schema = {
  type: 'object',
  properties: { case: { type: 'string' }, answer_path: { type: 'string' } },
  required: ['case', 'answer_path'],
  additionalProperties: false,
}
let active = 0
const waiters = []
async function slot(fn) {
  while (active >= MAX) await new Promise(resolve => waiters.push(resolve))
  active++
  try {
    return await fn()
  } finally {
    active--
    const wake = waiters.shift()
    if (wake) wake()
  }
}

const results = await parallel(A.cases.map(id => async () => {
  const prompt = `Run ${quote(`${A.factory}/bin/factory`)} eval bundle ${quote(id)} --answer-dir ${quote(A.run_dir)}, do what it prints, and return {case, answer_path}.`
  for (let attempt = 1; attempt <= 4; attempt++) {
    const label = `eval:${id}:attempt-${attempt}`
    const retry = attempt === 1 ? '' : `\nAttempt ${attempt}/4: the preceding attempt returned nothing. Reuse any completed answer on disk; do not discard partial work.`
    const result = await slot(() => agent(prompt + retry, { label, phase: 'Evaluate', schema }))
    if (result !== null && result !== undefined && result !== '') {
      if (result.case !== id || typeof result.answer_path !== 'string' || !result.answer_path) {
        throw new Error(`${label} returned an invalid result; correct the schema instead of retrying.`)
      }
      return { case: id, answer_path: result.answer_path, attempts: attempt }
    }
    log(`${label} returned nothing`)
  }
  return { case: id, answer_path: null, attempts: 4 }
}))
return { cases: A.cases, results }
