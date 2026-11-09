import test from 'node:test';
import assert from 'node:assert/strict';
import { tokenParts, statistics, rankConcentration, toolOutputBytes } from '../src/lib/overview.ts';

const turn = (overrides = {}) => ({
  id: 'a',
  input_tokens: 100,
  cached_input_tokens: 30,
  cache_write_input_tokens: 20,
  output_tokens: 10,
  token_total: 110,
  estimated_cost: null,
  tools: [],
  ...overrides,
});

test('partitions cache read and writes inside input without changing total', () => {
  assert.deepEqual(tokenParts(turn()), {
    uncached: 50,
    cached: 30,
    write: 20,
    output: 10,
    total: 110,
  });
  const unknown = tokenParts(turn({ cache_write_input_tokens: null }));
  assert.deepEqual(unknown, { uncached: 70, cached: 30, write: null, output: 10, total: 110 });
  assert.equal(unknown.uncached + unknown.cached + unknown.output, unknown.total);
  assert.equal(
    tokenParts(
      turn({
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_input_tokens: 0,
        output_tokens: 0,
        token_total: 0,
      }),
    ).total,
    0,
  );
});

test('keeps invalid or incomplete token partitions unknown', () => {
  for (const patch of [
    { cached_input_tokens: 101 },
    { cached_input_tokens: -1 },
    { input_tokens: true },
    { cache_write_input_tokens: 71 },
    { cache_write_input_tokens: -1 },
    { cached_input_tokens: null },
    { output_tokens: null },
    { token_total: 0 },
  ])
    assert.equal(tokenParts(turn(patch)), null);
});

test('uses type-7 quantiles and distinguishes zero from missing samples', () => {
  const stats = statistics([0, 1, 9, null, undefined, NaN, -1]);
  assert.equal(stats.count, 3);
  assert.equal(stats.median, 1);
  assert.ok(Math.abs(stats.p90 - 7.4) < 1e-12);
  assert.equal(stats.p10, 0.2);
  assert.equal(stats.mean, 10 / 3);
  assert.equal(statistics([null]).median, null);
  assert.equal(statistics([0]).median, 0);
});

test('concentration ranks the ceiling of ten percent of known samples only', () => {
  const turns = Array.from({ length: 11 }, (_, index) =>
    turn({ id: String(index), estimated_cost: index + 1 }),
  );
  turns.push(turn({ id: 'unknown', estimated_cost: null, token_total: 999999 }));
  const rank = rankConcentration(turns, 'cost');
  assert.deepEqual(
    rank.rows.map((row) => row.value),
    [11, 10],
  );
  assert.equal(rank.samples, 11);
  assert.equal(rank.total, 66);
  assert.equal(rank.share, 21 / 66);
  assert.equal(rankConcentration([turn({ estimated_cost: 0 })], 'cost').share, null);
  assert.equal(rankConcentration([], 'cost').total, null);
});

test('tool-byte concentration excludes unavailable outputs and never converts bytes to tokens', () => {
  assert.equal(toolOutputBytes(turn()), null);
  assert.equal(
    toolOutputBytes(turn({ tools: [{ output_bytes: 5 }, { output_bytes: null }] })),
    null,
  );
  assert.equal(toolOutputBytes(turn({ tools: [{ output_bytes: 0 }] })), 0);
  const rank = rankConcentration(
    [
      turn({ id: 'a', tools: [{ output_bytes: 5 }, { output_bytes: 10 }], token_total: 1 }),
      turn({ id: 'b', tools: [{ output_bytes: 5 }], token_total: 100000 }),
      turn({ id: 'c', tools: [{ output_bytes: null }] }),
    ],
    'toolBytes',
  );
  assert.equal(rank.samples, 2);
  assert.equal(rank.rows[0].turn.id, 'a');
  assert.equal(rank.share, 0.75);
});
