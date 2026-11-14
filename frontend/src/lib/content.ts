import type { ContentReference } from '../types';

export const contentLabels: Record<string, string> = {
  'message.user': '用户请求',
  'message.assistant': 'Agent 回复',
  'reasoning.summary': '推理摘要',
  'tool.called': '工具调用',
  'tool.returned': '工具结果',
  'turn.completed': '最终回答',
  'context.compacted': '上下文摘要',
};

export const bodyLabels: Record<string, string> = {
  text: '正文',
  arguments: '调用参数',
  output: '返回结果',
};

export function isContentReference(value: unknown): value is { content_ref: ContentReference } {
  if (!value || typeof value !== 'object' || !('content_ref' in value)) return false;
  const ref = value.content_ref;
  return (
    !!ref &&
    typeof ref === 'object' &&
    'path' in ref &&
    typeof ref.path === 'string' &&
    'sha256' in ref &&
    typeof ref.sha256 === 'string' &&
    'bytes' in ref &&
    typeof ref.bytes === 'number'
  );
}

export function pointerPart(value: string): string {
  return value.replace(/~/g, '~0').replace(/\//g, '~1');
}

export function contentReferences(
  value: unknown,
  pointer: string,
): { pointer: string; ref: ContentReference }[] {
  if (isContentReference(value)) return [{ pointer, ref: value.content_ref }];
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value).flatMap(([key, entry]) =>
    contentReferences(entry, `${pointer}/${pointerPart(key)}`),
  );
}

// References are opened through their archived event, never by a browser-supplied file path.
export function displayContent(value: unknown): string {
  if (typeof value === 'string') return value;
  return (
    JSON.stringify(
      value,
      (_key, entry: unknown) =>
        isContentReference(entry) ? `[外置正文 · ${entry.content_ref.bytes} 字节]` : entry,
      2,
    ) ?? ''
  );
}
