// Regression coverage for the JSON-wrapper rendering bug: a completed
// Workforce/DIRECT run's job.result can literally be a JSON-serialized
// string like '{"content":"hello\nworld"}' when a model echoes a
// response-wrapper shape it was never asked to produce. normalizeAssistantText
// unwraps exactly that one shape at the single choke point getFinalAnswer
// already funnels every render/copy/export path through, and leaves
// anything else -- prose, markdown, malformed JSON, or a legitimate JSON
// answer that isn't this wrapper -- untouched.
import { normalizeAssistantText, getFinalAnswer } from './src/stages.js'

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ` -- ${detail}`}`)
  if (!ok) failures++
}
const eq = (name, actual, expected) =>
  check(name, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`)

// --- normalizeAssistantText ---------------------------------------------

// 1. Raw normal markdown/string response -> unchanged.
const markdown = '# Heading\n\nSome **bold** text and a [link](https://example.com).'
eq('plain markdown passes through unchanged', normalizeAssistantText(markdown), markdown)

// 2. The exact reported wrapper shape -> unwrapped to its content.
eq(
  'JSON content-wrapper is unwrapped, escaped newline becomes a real newline',
  normalizeAssistantText('{"content":"hello\\nworld"}'),
  'hello\nworld',
)
eq(
  'wrapper with surrounding whitespace is still unwrapped',
  normalizeAssistantText('  {"content":"trimmed"}  '),
  'trimmed',
)
eq(
  'wrapper with extra sibling fields still unwraps the content field',
  normalizeAssistantText('{"content":"hi","model":"x","usage":{"tokens":5}}'),
  'hi',
)

// 3. Malformed JSON -> unchanged (never throws).
const malformed = '{"content":"unterminated'
eq('malformed JSON passes through unchanged', normalizeAssistantText(malformed), malformed)

// 4. Legitimate JSON answer that is not the response-wrapper shape -> unchanged.
const genuineJsonAnswer = '{"name":"Ada Lovelace","born":1815}'
eq(
  'legitimate JSON object without a string "content" field is left alone',
  normalizeAssistantText(genuineJsonAnswer),
  genuineJsonAnswer,
)
const contentIsNotAString = '{"content":{"nested":"object"}}'
eq(
  'a "content" field that is not a string is left alone (not the wrapper shape)',
  normalizeAssistantText(contentIsNotAString),
  contentIsNotAString,
)
const jsonArray = '[{"content":"item one"},{"content":"item two"}]'
eq('a JSON array (not an object) is left alone', normalizeAssistantText(jsonArray), jsonArray)

// Not aggressive: text that merely starts/ends with braces without being
// valid JSON, or braces embedded in prose, must never be touched.
const braceProse = '{this is not json, just a sentence in braces}'
eq('brace-shaped prose that is not valid JSON is untouched', normalizeAssistantText(braceProse), braceProse)

// Non-string input (defensive) is returned as-is.
eq('non-string input passes through unchanged', normalizeAssistantText(null), null)
eq('non-string input passes through unchanged (number)', normalizeAssistantText(42), 42)

// --- getFinalAnswer applies the same normalization at the canonical boundary

const wrappedResultJob = {
  job_id: 'j1',
  status: 'done',
  result: '{"content":"hello\\nworld"}',
}
eq(
  'getFinalAnswer normalizes a JSON-wrapped job.result for a completed run',
  getFinalAnswer(wrappedResultJob, []),
  'hello\nworld',
)

const plainResultJob = {
  job_id: 'j2',
  status: 'done',
  result: 'Just a normal completed answer.',
}
eq(
  'getFinalAnswer leaves a normal completed answer unchanged',
  getFinalAnswer(plainResultJob, []),
  'Just a normal completed answer.',
)

// 5. History/reload uses the same normalized value as live rendering: a job
// object hydrated from GET /jobs (history) and one delivered live via SSE
// carry the exact same shape, so calling getFinalAnswer on either produces
// an identical, already-normalized result -- there is no separate history
// rendering path to fall out of sync.
const liveJob = { job_id: 'j3', status: 'done', result: '{"content":"reload me\\nplease"}' }
const historyHydratedJob = JSON.parse(JSON.stringify(liveJob)) // simulates a round-trip through GET /jobs
eq(
  'history/reload hydration normalizes identically to live rendering',
  getFinalAnswer(historyHydratedJob, []),
  getFinalAnswer(liveJob, []),
)
eq(
  'history/reload result is the unwrapped content, not the raw JSON wrapper',
  getFinalAnswer(historyHydratedJob, []),
  'reload me\nplease',
)

console.log(failures ? `\n${failures} FAILED` : '\nAll assistant-result normalization checks passed')
process.exit(failures ? 1 : 0)
