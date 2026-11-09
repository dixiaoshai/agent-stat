export type SessionNode<T> = {
  session: T;
  children: SessionNode<T>[];
  parentMissing: boolean;
};

/** Group already-sorted sessions by their namespaced parent ID. */
export function buildSessionTree<T extends { id: string; parent_id?: string | null }>(
  sessions: T[],
  matches?: Set<string>,
): SessionNode<T>[] {
  const nodes = new Map<string, SessionNode<T>>();
  const order = new Map<string, number>();
  const parents = new Map<string, string>();
  sessions.forEach((session, index) => {
    nodes.set(session.id, { session, children: [], parentMissing: false });
    order.set(session.id, index);
  });
  for (const session of sessions) {
    if (!session.parent_id) continue;
    if (nodes.has(session.parent_id)) parents.set(session.id, session.parent_id);
    else nodes.get(session.id)!.parentMissing = true;
  }

  // Break cycles deterministically at the earliest sorted member. Every
  // session remains visible; a malformed link is not a missing parent.
  const visited = new Set<string>();
  for (const session of sessions) {
    if (visited.has(session.id)) continue;
    const chain: string[] = [];
    const positions = new Map<string, number>();
    let current: string | undefined = session.id;
    while (current && !visited.has(current) && !positions.has(current)) {
      positions.set(current, chain.length);
      chain.push(current);
      current = parents.get(current);
    }
    if (current && positions.has(current)) {
      const cycle = chain.slice(positions.get(current));
      const root = cycle.reduce((a, b) => (order.get(a)! < order.get(b)! ? a : b));
      parents.delete(root);
    }
    for (const id of chain) visited.add(id);
  }

  const visible = new Set<string>();
  if (matches === undefined) {
    for (const id of nodes.keys()) visible.add(id);
  } else {
    for (const id of matches) {
      let current: string | undefined = nodes.has(id) ? id : undefined;
      while (current && !visible.has(current)) {
        visible.add(current);
        current = parents.get(current);
      }
    }
  }

  const roots: SessionNode<T>[] = [];
  for (const session of sessions) {
    if (!visible.has(session.id)) continue;
    const node = nodes.get(session.id)!;
    const parent = parents.get(session.id);
    if (parent && visible.has(parent)) nodes.get(parent)!.children.push(node);
    else roots.push(node);
  }
  return roots;
}
