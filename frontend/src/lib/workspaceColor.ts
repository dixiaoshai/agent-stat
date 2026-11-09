import type { CSSProperties } from 'react';

/** Keep a Codex project's color stable across sorting, filtering and worktrees. */
export function workspaceColor(project: {
  project?: string | null;
  project_name?: string | null;
}): CSSProperties {
  const key = project.project_name?.trim() || project.project || '';
  let hash = 2166136261;
  for (const character of key) hash = Math.imul(hash ^ character.codePointAt(0)!, 16777619);
  return {
    '--workspace-hue': String(((hash >>> 0) % 36000) / 100),
    '--workspace-saturation': key ? '44%' : '10%',
  } as CSSProperties;
}
