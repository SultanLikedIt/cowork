import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { stableStreamingMarkdown } from './src/stages.js'

// Mirror of MARKDOWN_COMPONENTS (server-render friendly: no hooks/clipboard).
function CodeBlock({ node, className, children, ...rest }) {
  const source = String(children ?? '')
  const language = /language-([\w+-]+)/.exec(className || '')?.[1] || null
  if (!language && !source.includes('\n')) {
    return React.createElement('code', { className, ...rest }, children)
  }
  return React.createElement('div', { className: 'code-block-container' },
    React.createElement('div', { className: 'code-block-header' },
      React.createElement('span', { className: 'code-block-lang' }, language || 'text'),
      React.createElement('button', { className: 'code-block-copy' }, 'Copy')),
    React.createElement('pre', { className: 'code-block-pre' },
      React.createElement('code', { className, ...rest }, children)))
}
const components = { code: CodeBlock, pre: ({ children }) => React.createElement(React.Fragment, null, children) }
const render = (md) => renderToStaticMarkup(
  React.createElement(ReactMarkdown, { remarkPlugins: [remarkGfm], components }, md))

const doc = [
  'Here is a **FastAPI** service:', '',
  '```python', 'from fastapi import FastAPI', '', 'app = FastAPI()', '',
  '@app.get("/health")', 'def health():', '    return {"status": "ok"}', '```', '',
  'Then the manifest:', '',
  '```yaml', 'apiVersion: apps/v1', 'kind: Deployment', 'metadata:',
  '  name: api', 'spec:', '  replicas: 2', '```', '',
  'Run it with `uvicorn` and see the `/health` route.', '',
  '- list item one', '- list item two', '', '## A heading', '', 'Closing prose.',
].join('\n')

const html = render(doc)
const checks = [
  ['two fenced blocks became code-block containers', (html.match(/class="code-block-container"/g)||[]).length === 2],
  ['python language label', html.includes('>python<')],
  ['yaml language label', html.includes('>yaml<')],
  ['copy button per block', (html.match(/code-block-copy/g)||[]).length === 2],
  ['no nested <pre><pre>', !/<pre[^>]*>\s*<pre/.test(html)],
  ['newlines preserved in python block', html.includes('from fastapi import FastAPI\n')],
  ['indentation preserved (4-space return)', html.includes('\n    return {&quot;status&quot;: &quot;ok&quot;}')],
  ['yaml 2-space indent preserved', html.includes('\n  name: api')],
  ['inline code stays plain <code>', html.includes('<code>uvicorn</code>')],
  ['inline code not wrapped in container', !/<div class="code-block-container">\s*<div class="code-block-header">\s*<span class="code-block-lang">text/.test(html)],
  ['prose before code', html.includes('<p>Here is a <strong>FastAPI</strong> service:</p>')],
  ['prose after code', html.includes('<p>Closing prose.</p>')],
  ['list survived', html.includes('<li>list item one</li>')],
  ['heading survived', html.includes('<h2>A heading</h2>')],
]
let fail = 0
for (const [name, ok] of checks) { console.log(`[${ok?'PASS':'FAIL'}] ${name}`); if(!ok) fail++ }

// Streaming parity uses the REAL exported function, not a copy.
let unterminated = 0, mergedTail = 0
for (let i = 1; i <= doc.length; i++) {
  const frame = stableStreamingMarkdown(doc.slice(0, i))
  const fences = (frame.match(/```/g) || []).length
  if (fences % 2 !== 0) unterminated++
  const out = render(frame)
  // Prose that has already streamed must never be swallowed into code.
  if (i > doc.indexOf('Then the manifest:') + 18 && out.includes('Then the manifest:') && !out.includes('<p>Then the manifest:</p>')) mergedTail++
}
console.log(`[${unterminated===0?'PASS':'FAIL'}] no streamed frame has an unterminated fence (${doc.length} prefixes)`)
console.log(`[${mergedTail===0?'PASS':'FAIL'}] streamed prose never absorbed into a code block`)
if (unterminated) fail++
if (mergedTail) fail++

// Final frame must equal the settled render exactly.
const same = render(stableStreamingMarkdown(doc)) === html
console.log(`[${same?'PASS':'FAIL'}] final streamed frame renders identically to settled answer`)
if (!same) fail++
console.log(fail ? `\n${fail} FAILED` : '\nAll markdown/code rendering checks passed')
process.exit(fail ? 1 : 0)
