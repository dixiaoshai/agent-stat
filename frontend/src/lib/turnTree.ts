import { buildSessionTree } from './sessionTree.ts';

export type TurnNode<T> = {
  turn: T;
  children: TurnNode<T>[];
  parentMissing: boolean;
};

/** Attach already-sorted turns only by their namespaced parent turn ID. */
export function buildTurnTree<T extends { id: string; parent_turn_id?: string | null }>(
  turns: T[],
  matches?: Set<string>,
): TurnNode<T>[] {
  // Reuse the cycle, visibility, and stable-order handling without adding a
  // synthetic parent_id to a Turn or changing the caller's objects.
  const sessions = turns.map((turn) => ({
    id: turn.id,
    parent_id: turn.parent_turn_id,
    turn,
  }));
  const tree = buildSessionTree(sessions, matches);
  const roots: TurnNode<T>[] = tree.map((node) => ({
    turn: node.session.turn,
    children: [],
    parentMissing: node.parentMissing,
  }));
  const pending = tree.map((source, index) => ({ source, target: roots[index] }));
  // Conversion remains safe for deeply nested subagents.
  while (pending.length) {
    const { source, target } = pending.pop()!;
    for (const child of source.children) {
      const converted: TurnNode<T> = {
        turn: child.session.turn,
        children: [],
        parentMissing: child.parentMissing,
      };
      target.children.push(converted);
      pending.push({ source: child, target: converted });
    }
  }
  return roots;
}
