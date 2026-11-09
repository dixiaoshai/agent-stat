import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSessionTree } from '../src/lib/sessionTree.ts';

const session = (id, parent_id = null) => ({ id, parent_id });
const shape = (nodes) =>
  nodes.map(({ session: value, children, parentMissing }) => ({
    id: value.id,
    parentMissing,
    children: shape(children),
  }));
const node = (id, children = [], parentMissing = false) => ({ id, parentMissing, children });

test('attaches children that sort before parents and preserves sibling and root order', () => {
  const sessions = [
    session('child-b', 'parent'),
    session('other-root'),
    session('child-a', 'parent'),
    session('parent'),
    session('grandchild', 'child-b'),
    session('last-root'),
  ];
  assert.deepEqual(shape(buildSessionTree(sessions)), [
    node('other-root'),
    node('parent', [node('child-b', [node('grandchild')]), node('child-a')]),
    node('last-root'),
  ]);
});

test('keeps an out-of-scope parent child visible and marks only the missing parent', () => {
  assert.deepEqual(
    shape(
      buildSessionTree([
        session('orphan', 'outside'),
        session('grandchild', 'orphan'),
        session('regular-root'),
      ]),
    ),
    [node('orphan', [node('grandchild')], true), node('regular-root')],
  );
});

test('breaks self-references and longer cycles without losing or duplicating sessions', () => {
  const sessions = [
    session('self', 'self'),
    session('cycle-c', 'cycle-b'),
    session('cycle-a', 'cycle-c'),
    session('cycle-b', 'cycle-a'),
    session('leaf', 'cycle-b'),
    session('regular-root'),
  ];
  assert.deepEqual(shape(buildSessionTree(sessions)), [
    node('self'),
    node('cycle-c', [node('cycle-a', [node('cycle-b', [node('leaf')])])]),
    node('regular-root'),
  ]);
});

test('search includes matching sessions and their ancestors without unrelated siblings', () => {
  const sessions = [
    session('parent'),
    session('other-child', 'parent'),
    session('child', 'parent'),
    session('grandchild', 'child'),
    session('other-root'),
  ];
  assert.deepEqual(shape(buildSessionTree(sessions, new Set(['grandchild']))), [
    node('parent', [node('child', [node('grandchild')])]),
  ]);
  assert.deepEqual(shape(buildSessionTree(sessions, new Set(['parent', 'other-root']))), [
    node('parent'),
    node('other-root'),
  ]);
  assert.deepEqual(buildSessionTree(sessions, new Set()), []);
  assert.deepEqual(buildSessionTree(sessions, new Set(['absent'])), []);
});

test('search terminates on malformed cycles and retains the selected chain', () => {
  const sessions = [session('a', 'b'), session('b', 'c'), session('c', 'a')];
  assert.deepEqual(shape(buildSessionTree(sessions, new Set(['b']))), [
    node('a', [node('c', [node('b')])]),
  ]);
});

test('uses complete namespaced IDs when native IDs repeat across instances', () => {
  const aParent = '["codex","device-a","same-parent"]';
  const bParent = '["codex","device-b","same-parent"]';
  const aChild = '["codex","device-a","same-child"]';
  const bChild = '["codex","device-b","same-child"]';
  assert.deepEqual(
    shape(
      buildSessionTree([
        session(aChild, aParent),
        session(bParent),
        session(bChild, bParent),
        session(aParent),
      ]),
    ),
    [node(bParent, [node(bChild)]), node(aParent, [node(aChild)])],
  );
});
