import test from 'node:test';
import assert from 'node:assert/strict';
import { contentReferences, displayContent, pointerPart } from '../src/lib/content.ts';

const reference = { path: '/private/blobs/output.txt', bytes: 120, sha256: 'a'.repeat(64), media_type: 'text/plain' };

test('resolves nested body reference pointers including escaped property names', () => {
  const body = { 'a/b~c': [{ content_ref: reference }], ordinary: 'unchanged' };
  assert.deepEqual(contentReferences(body, ''), [{ pointer: '/a~1b~0c/0', ref: reference }]);
  assert.equal(pointerPart('a/b~c'), 'a~1b~0c');
});

test('shows file metadata without turning local paths into browser links', () => {
  const text = displayContent({ result: { content_ref: reference }, status: '[REDACTED:secret]' });
  assert.ok(text.includes('外置正文'));
  assert.ok(text.includes('120'));
  assert.ok(text.includes('[REDACTED:secret]'));
  assert.ok(!text.includes('/private/blobs'));
});

test('preserves literal content and unstructured tool output', () => {
  const value = '<script>alert(1)</script>\n[link](javascript:alert(2))';
  assert.equal(displayContent(value), value);
  assert.equal(displayContent(null), 'null');
  assert.deepEqual(contentReferences({ output: value }, ''), []);
});
