import type { SessionNode } from './sessionTree.ts';
import type { TurnNode } from './turnTree.ts';

/** One turn row and its nest depth, as rendered by the turn table. */
export type TurnEntry<T> = { node: TurnNode<T>; depth: number };

/** Flatten turn nodes depth-first; a collapsed turn hides its descendants. */
export function flattenTurns<T extends { id: string }>(
  tree: TurnNode<T>[],
  expanded?: Set<string>,
  depth = 0,
): TurnEntry<T>[] {
  const entries: TurnEntry<T>[] = [];
  const pending = tree.map((node) => ({ node, depth })).reverse();
  while (pending.length) {
    const entry = pending.pop()!;
    entries.push(entry);
    if (!expanded || expanded.has(entry.node.turn.id)) {
      for (let i = entry.node.children.length - 1; i >= 0; i -= 1) {
        pending.push({ node: entry.node.children[i], depth: entry.depth + 1 });
      }
    }
  }
  return entries;
}

/** Minimal session shape the row flattener needs; testable with plain objects. */
export type SessionRowLike = { id: string; parent_id?: string | null; turns?: number };

export type SessionRow<S> = {
  kind: 'session';
  session: S;
  depth: number;
  /** Number of top-level turn nodes rendered under this session. */
  turnCount: number;
  /** Direct child sessions that render as nested rows (hidden children promoted). */
  childSessions: SessionNode<S>[];
  parentMissing: boolean;
};

export type SessionTurnRow<T> = { kind: 'turn'; node: TurnNode<T>; depth: number };

export type SessionRowEntry<S, T> = SessionRow<S> | SessionTurnRow<T>;

/**
 * A child session whose turns all nest under parent turn rows must not get its
 * own session row, or turn-level nesting (Codex) would render it twice. A child
 * with zero turns still renders so empty sessions stay visible.
 */
function hidesSessionRow<T>(session: SessionRowLike, roots: TurnNode<T>[]): boolean {
  return Boolean(session.parent_id) && roots.length === 0 && (session.turns ?? 0) > 0;
}

/** Visible children of a node, promoting the children of hidden child sessions. */
function visibleChildren<S extends SessionRowLike, T extends { id: string }>(
  node: SessionNode<S>,
  rootsBySession: Map<string, TurnNode<T>[]>,
): SessionNode<S>[] {
  const children: SessionNode<S>[] = [];
  const pending = node.children.slice().reverse();
  while (pending.length) {
    const child = pending.pop()!;
    const roots = rootsBySession.get(child.session.id) ?? [];
    if (hidesSessionRow(child.session, roots)) {
      for (let i = child.children.length - 1; i >= 0; i -= 1) pending.push(child.children[i]);
      continue;
    }
    children.push(child);
  }
  return children;
}

/**
 * Flatten a session hierarchy into render order: each session row, then (when
 * expanded) its own top-level turns followed by its child session rows.
 */
export function flattenSessionRows<S extends SessionRowLike, T extends { id: string }>(
  tree: SessionNode<S>[],
  rootsBySession: Map<string, TurnNode<T>[]>,
  expanded: Set<string>,
): SessionRowEntry<S, T>[] {
  const rows: SessionRowEntry<S, T>[] = [];
  const pending = tree.map((node) => ({ node, depth: 0 })).reverse();
  while (pending.length) {
    const { node, depth } = pending.pop()!;
    const roots = rootsBySession.get(node.session.id) ?? [];
    if (hidesSessionRow(node.session, roots)) {
      for (let i = node.children.length - 1; i >= 0; i -= 1) {
        pending.push({ node: node.children[i], depth });
      }
      continue;
    }
    const childSessions = visibleChildren(node, rootsBySession);
    rows.push({
      kind: 'session',
      session: node.session,
      depth,
      turnCount: roots.length,
      childSessions,
      parentMissing: node.parentMissing,
    });
    if (!expanded.has(node.session.id)) continue;
    for (const entry of flattenTurns(roots, expanded, depth + 1)) {
      rows.push({ kind: 'turn', node: entry.node, depth: entry.depth });
    }
    for (let i = childSessions.length - 1; i >= 0; i -= 1) {
      pending.push({ node: childSessions[i], depth: depth + 1 });
    }
  }
  return rows;
}

/** Count session rows the hierarchy would render, hidden children excluded. */
export function sessionRowCount<S extends SessionRowLike, T extends { id: string }>(
  tree: SessionNode<S>[],
  rootsBySession: Map<string, TurnNode<T>[]>,
): number {
  let count = 0;
  const pending = [...tree];
  while (pending.length) {
    const node = pending.pop()!;
    const roots = rootsBySession.get(node.session.id) ?? [];
    if (hidesSessionRow(node.session, roots)) {
      pending.push(...node.children);
      continue;
    }
    count += 1;
    pending.push(...visibleChildren(node, rootsBySession));
  }
  return count;
}
