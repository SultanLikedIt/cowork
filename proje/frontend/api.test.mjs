import { formatErrorDetail } from './src/api.js'

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ` -- ${detail}`}`)
  if (!ok) failures++
}
const eq = (name, actual, expected) =>
  check(
    name,
    actual === expected,
    `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`,
  )

eq('string detail passes through', formatErrorDetail('too large', 'fallback'), 'too large')
eq(
  'fastapi 422 array uses msg',
  formatErrorDetail(
    [{ loc: ['body', 'files'], msg: 'Field required', type: 'missing' }],
    'fallback',
  ),
  'Field required',
)
eq(
  'multiple msgs join',
  formatErrorDetail(
    [
      { loc: ['body', 'files'], msg: 'Field required', type: 'missing' },
      { loc: ['body', 'files', 0], msg: 'file too large', type: 'value_error' },
    ],
    'fallback',
  ),
  'Field required; file too large',
)
eq(
  'object with msg',
  formatErrorDetail({ msg: 'Unsupported file type' }, 'fallback'),
  'Unsupported file type',
)
eq(
  'docling structured detail uses message and reason',
  formatErrorDetail(
    {
      reason: 'docling_conversion_failed',
      message: 'Could not parse this document.',
      retryable: false,
    },
    'fallback',
  ),
  'Could not parse this document. (docling_conversion_failed)',
)
eq(
  'docling message without reason',
  formatErrorDetail({ message: 'Parse timed out.' }, 'fallback'),
  'Parse timed out.',
)
eq('null uses fallback', formatErrorDetail(null, 'fallback'), 'fallback')
eq(
  'plain object without msg uses fallback',
  formatErrorDetail({ loc: ['body'] }, 'fallback'),
  'fallback',
)

if (failures) {
  console.error(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nAll api detail checks passed')
