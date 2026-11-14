import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSessionTree } from '../src/lib/sessionTree.ts';
import { buildTurnTree } from '../src/lib/turnTree.ts';
import { flattenSessionRows, flattenTurns, sessionRowCount } from '../src/lib/sessionRows.ts';

const session = (id, parent_id = null, turns = 1) => ({ id, parent_id, turns });
const turn = (id, parent_turn_id = null, session_id = null) => ({
  id,
  parent_turn_id,
  session_id,
});

const rootsBySession = (turns) => {
  const map = new Map();
  for (const node of buildTurnTree(turns)) {
    const key = node.turn.session_id || '';
    map.set(key, [...(map.get(key) || []), node]);
  }
  return map;
};

const rowsFor = (sessions, turns, expanded = []) =>
  flattenSessionRows(buildSessionTree(sessions), rootsBySession(turns), new Set(expanded));

const shape = (rows) =>
  rows.map((row) =>
    row.kind === 'session'
      ? {
          session: row.session.id,
          depth: row.depth,
          turnCount: row.turnCount,
          children: row.childSessions.map((child) => child.session.id),
          parentMissing: row.parentMissing,
        }
      : { turn: row.node.turn.id, depth: row.depth },
  );

const sessionRow = (id, depth, turnCount, children = [], parentMissing = false) => ({
  session: id,
  depth,
  turnCount,
  children,
  parentMissing,
});
const turnRow = (id, depth) => ({ turn: id, depth });

test('renders sorted roots first and nests children under collapsed parents', () => {
  const sessions = [
    session('child', 'parent'),
    session('root-b'),
    session('parent', null, 2),
    session('root-a', null, 0),
  ];
  const turns = [
    turn('p-turn-1', null, 'parent'),
    turn('p-turn-2', null, 'parent'),
    turn('c-turn', null, 'child'),
    turn('b-turn', null, 'root-b'),
  ];
  assert.deepEqual(shape(rowsFor(sessions, turns)), [
    sessionRow('root-b', 0, 1),
    sessionRow('parent', 0, 2, ['child']),
    sessionRow('root-a', 0, 0),
  ]);
  // Collapsed parent hides its child session entirely.
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent'])), [
    sessionRow('root-b', 0, 1),
    sessionRow('parent', 0, 2, ['child']),
    turnRow('p-turn-1', 1),
    turnRow('p-turn-2', 1),
    sessionRow('child', 1, 1),
    sessionRow('root-a', 0, 0),
  ]);
});

test('expands a nested child session one level deeper than its parent', () => {
  const sessions = [session('parent', null, 1), session('child', 'parent')];
  const turns = [turn('p-turn', null, 'parent'), turn('c-turn', null, 'child')];
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent', 'child'])), [
    sessionRow('parent', 0, 1, ['child']),
    turnRow('p-turn', 1),
    sessionRow('child', 1, 1),
    turnRow('c-turn', 2),
  ]);
});

test('renders a nested child session with zero turns as a row', () => {
  const sessions = [session('parent', null, 2), session('empty-child', 'parent', 0)];
  const turns = [turn('p-turn-1', null, 'parent'), turn('p-turn-2', null, 'parent')];
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent'])), [
    sessionRow('parent', 0, 2, ['empty-child']),
    turnRow('p-turn-1', 1),
    turnRow('p-turn-2', 1),
    sessionRow('empty-child', 1, 0),
  ]);
});

test('hides a child session whose turns all nest under parent turn rows', () => {
  const sessions = [session('parent', null, 1), session('codex-child', 'parent', 1)];
  const turns = [turn('p-turn', null, 'parent'), turn('c-turn', 'p-turn', 'codex-child')];
  // The child turn renders under the parent turn; no duplicate child session row.
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent', 'p-turn'])), [
    sessionRow('parent', 0, 1),
    turnRow('p-turn', 1),
    turnRow('c-turn', 2),
  ]);
  const parentExpanded = shape(rowsFor(sessions, turns, ['parent']));
  assert.deepEqual(parentExpanded, [sessionRow('parent', 0, 1), turnRow('p-turn', 1)]);
  assert.equal(sessionRowCount(buildSessionTree(sessions), rootsBySession(turns)), 1);
});

test('shows a child session with unlinked turns without duplicating linked ones', () => {
  const sessions = [session('parent', null, 2), session('child', 'parent', 2)];
  const turns = [
    turn('p-turn-1', null, 'parent'),
    turn('p-turn-2', null, 'parent'),
    turn('c-linked', 'p-turn-1', 'child'),
    turn('c-free', null, 'child'),
  ];
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent'])), [
    sessionRow('parent', 0, 2, ['child']),
    turnRow('p-turn-1', 1),
    turnRow('p-turn-2', 1),
    sessionRow('child', 1, 1),
  ]);
  // Expanding the child shows only the unlinked turn.
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent', 'child'])), [
    sessionRow('parent', 0, 2, ['child']),
    turnRow('p-turn-1', 1),
    turnRow('p-turn-2', 1),
    sessionRow('child', 1, 1),
    turnRow('c-free', 2),
  ]);
  // The linked turn stays nested under its parent turn and renders once.
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent', 'p-turn-1'])), [
    sessionRow('parent', 0, 2, ['child']),
    turnRow('p-turn-1', 1),
    turnRow('c-linked', 2),
    turnRow('p-turn-2', 1),
    sessionRow('child', 1, 1),
  ]);
});

test('keeps a child session visible when its parent is outside the filtered set', () => {
  const sessions = [session('orphan', 'outside'), session('regular')];
  const turns = [turn('o-turn', null, 'orphan'), turn('r-turn', null, 'regular')];
  assert.deepEqual(shape(rowsFor(sessions, turns)), [
    sessionRow('orphan', 0, 1, [], true),
    sessionRow('regular', 0, 1),
  ]);
  assert.deepEqual(shape(rowsFor(sessions, turns, ['orphan'])), [
    sessionRow('orphan', 0, 1, [], true),
    turnRow('o-turn', 1),
    sessionRow('regular', 0, 1),
  ]);
});

test('keeps increasing depth down a deep session chain and collapses below', () => {
  const sessions = [
    session('s0', null, 1),
    session('s1', 's0', 1),
    session('s2', 's1', 1),
    session('s3', 's2', 1),
  ];
  const turns = [0, 1, 2, 3].map((index) => turn(`t${index}`, null, `s${index}`));
  assert.deepEqual(shape(rowsFor(sessions, turns, ['s0', 's1', 's2', 's3'])), [
    sessionRow('s0', 0, 1, ['s1']),
    turnRow('t0', 1),
    sessionRow('s1', 1, 1, ['s2']),
    turnRow('t1', 2),
    sessionRow('s2', 2, 1, ['s3']),
    turnRow('t2', 3),
    sessionRow('s3', 3, 1),
    turnRow('t3', 4),
  ]);
  assert.deepEqual(shape(rowsFor(sessions, turns, ['s0', 's1'])), [
    sessionRow('s0', 0, 1, ['s1']),
    turnRow('t0', 1),
    sessionRow('s1', 1, 1, ['s2']),
    turnRow('t1', 2),
    sessionRow('s2', 2, 1, ['s3']),
  ]);
});

test('promotes children of a hidden child session to the nearest rendered parent', () => {
  const sessions = [
    session('parent', null, 1),
    session('hidden-child', 'parent', 1),
    session('grandchild', 'hidden-child'),
  ];
  const turns = [
    turn('p-turn', null, 'parent'),
    turn('h-turn', 'p-turn', 'hidden-child'),
    turn('g-turn', null, 'grandchild'),
  ];
  assert.deepEqual(shape(rowsFor(sessions, turns, ['parent', 'grandchild'])), [
    sessionRow('parent', 0, 1, ['grandchild']),
    turnRow('p-turn', 1),
    sessionRow('grandchild', 1, 1),
    turnRow('g-turn', 2),
  ]);
  assert.equal(sessionRowCount(buildSessionTree(sessions), rootsBySession(turns)), 2);
});

test('renders a search ancestor chain from a filtered session tree', () => {
  const sessions = [
    session('root', null, 0),
    session('unrelated-root', null, 0),
    session('mid', 'root'),
    session('leaf', 'mid'),
  ];
  const filtered = buildSessionTree(sessions, new Set(['leaf']));
  const rows = flattenSessionRows(
    filtered,
    rootsBySession([turn('m-turn', null, 'mid'), turn('l-turn', null, 'leaf')]),
    new Set(['root', 'mid']),
  );
  assert.deepEqual(shape(rows), [
    sessionRow('root', 0, 0, ['mid']),
    sessionRow('mid', 1, 1, ['leaf']),
    turnRow('m-turn', 2),
    sessionRow('leaf', 2, 1),
  ]);
});

test('flattens turn trees with collapsed branches hidden', () => {
  const tree = buildTurnTree([
    turn('root', null, 's'),
    turn('child', 'root', 's'),
    turn('grandchild', 'child', 's'),
  ]);
  const shape = (entries) => entries.map(({ node, depth }) => [node.turn.id, depth]);
  assert.deepEqual(shape(flattenTurns(tree)), [
    ['root', 0],
    ['child', 1],
    ['grandchild', 2],
  ]);
  assert.deepEqual(shape(flattenTurns(tree, new Set())), [['root', 0]]);
  assert.deepEqual(shape(flattenTurns(tree, new Set(['root', 'child']))), [
    ['root', 0],
    ['child', 1],
    ['grandchild', 2],
  ]);
  assert.deepEqual(shape(flattenTurns(tree, new Set(['root']))), [
    ['root', 0],
    ['child', 1],
  ]);
  assert.deepEqual(shape(flattenTurns(tree, new Set(), 3)), [['root', 3]]);
});
