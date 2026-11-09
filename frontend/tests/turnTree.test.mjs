import test from 'node:test';
import assert from 'node:assert/strict';
import { buildTurnTree } from '../src/lib/turnTree.ts';

const turn = (id, parent_turn_id = null, session_id = null) => ({
  id,
  parent_turn_id,
  session_id,
});
const node = (id, children = [], parentMissing = false) => ({ id, parentMissing, children });
const shape = (nodes) =>
  nodes.map(({ turn: value, children, parentMissing }) => ({
    id: value.id,
    parentMissing,
    children: shape(children),
  }));

test('attaches separate turns from the same child session to the correct parent turns', () => {
  const first = turn('child-turn-1', 'parent-turn-1', 'child-session');
  const second = turn('child-turn-2', 'parent-turn-2', 'child-session');
  const turns = [second, turn('parent-turn-1'), first, turn('parent-turn-2')];
  const tree = buildTurnTree(turns);
  assert.deepEqual(shape(tree), [
    node('parent-turn-1', [node('child-turn-1')]),
    node('parent-turn-2', [node('child-turn-2')]),
  ]);
  assert.strictEqual(tree[0].children[0].turn, first);
  assert.strictEqual(tree[1].children[0].turn, second);
  assert.equal('parent_id' in first, false);
});

test('retains multiple levels, sibling order, and disconnected parent IDs', () => {
  const turns = [
    turn('grandchild', 'child-a'),
    turn('child-b', 'parent'),
    turn('orphan', 'absent'),
    turn('child-a', 'parent'),
    turn('parent'),
  ];
  assert.deepEqual(shape(buildTurnTree(turns)), [
    node('orphan', [], true),
    node('parent', [node('child-b'), node('child-a', [node('grandchild')])]),
  ]);
});

test('matches complete turn IDs across namespaced instances', () => {
  const a = '["device-a","same-turn"]';
  const b = '["device-b","same-turn"]';
  const childA = '["device-a","child"]';
  const childB = '["device-b","child"]';
  assert.deepEqual(shape(buildTurnTree([turn(childA, a), turn(b), turn(childB, b), turn(a)])), [
    node(b, [node(childB)]),
    node(a, [node(childA)]),
  ]);
});

test('does not guess a parent from a shared session or timing when attribution is absent', () => {
  const unrelated = { ...turn('unknown', null, 'child'), started_at: '2026-11-08T10:00:00Z' };
  const parent = { ...turn('parent', null, 'parent'), started_at: '2026-11-08T09:59:59Z' };
  assert.deepEqual(shape(buildTurnTree([parent, unrelated])), [node('parent'), node('unknown')]);
});

test('search shows matching turns and ancestor chain while excluding siblings', () => {
  const turns = [
    turn('parent'),
    turn('sibling', 'parent'),
    turn('child', 'parent'),
    turn('grandchild', 'child'),
    turn('other'),
  ];
  assert.deepEqual(shape(buildTurnTree(turns, new Set(['grandchild']))), [
    node('parent', [node('child', [node('grandchild')])]),
  ]);
  assert.deepEqual(shape(buildTurnTree(turns, new Set(['parent', 'other']))), [
    node('parent'),
    node('other'),
  ]);
  assert.deepEqual(buildTurnTree(turns, new Set()), []);
  assert.deepEqual(buildTurnTree(turns, new Set(['absent'])), []);
});

test('safely breaks self-references and longer cycles while retaining every turn', () => {
  const turns = [
    turn('self', 'self'),
    turn('cycle-c', 'cycle-b'),
    turn('cycle-a', 'cycle-c'),
    turn('cycle-b', 'cycle-a'),
    turn('leaf', 'cycle-b'),
    turn('other'),
  ];
  assert.deepEqual(shape(buildTurnTree(turns)), [
    node('self'),
    node('cycle-c', [node('cycle-a', [node('cycle-b', [node('leaf')])])]),
    node('other'),
  ]);
  assert.deepEqual(shape(buildTurnTree(turns, new Set(['leaf']))), [
    node('cycle-c', [node('cycle-a', [node('cycle-b', [node('leaf')])])]),
  ]);
});

test('builds a deep chain without recursive traversal', () => {
  const turns = Array.from({ length: 20000 }, (_, index) =>
    turn(String(index), index ? String(index - 1) : null),
  );
  const tree = buildTurnTree(turns);
  assert.equal(tree.length, 1);
  let current = tree[0];
  for (let index = 0; index < turns.length; index++) {
    assert.strictEqual(current.turn, turns[index]);
    assert.equal(current.children.length, index === turns.length - 1 ? 0 : 1);
    current = current.children[0];
  }
});
