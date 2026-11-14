import { useEffect, useRef, useState } from 'react';
import type { ContentEvent, ContentReference, SessionContent } from '../types';
import { getContentBlob, getSessionContent } from '../lib/api';
import {
  bodyLabels,
  contentLabels,
  contentReferences,
  displayContent,
  isContentReference,
  pointerPart,
} from '../lib/content';
import { formatDate, formatNumber } from '../lib/format';

export function ContentTimeline({ sessionId, turnId }: { sessionId: string; turnId?: string }) {
  const [page, setPage] = useState<SessionContent | null>(null);
  const [events, setEvents] = useState<ContentEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function load(more = false) {
    controller.current?.abort();
    const pending = new AbortController();
    controller.current = pending;
    setLoading(true);
    setError(null);
    if (!more) {
      setPage(null);
      setEvents([]);
    }
    try {
      const result = await getSessionContent(
        sessionId,
        turnId,
        more ? page?.next_cursor : 0,
        pending.signal,
        more && page?.archive_id && page.revision
          ? { archive_id: page.archive_id, revision: page.revision }
          : undefined,
      );
      if (pending.signal.aborted) return;
      if (more && result.archive_id !== page?.archive_id) {
        throw new Error('正文归档已更换，请重新加载。');
      }
      setPage(result);
      setEvents((previous) => {
        const combined = more ? [...previous, ...result.events] : result.events;
        return [...new Map(combined.map((event) => [event.event_id, event])).values()];
      });
    } catch (reason) {
      if (!pending.signal.aborted)
        setError(reason instanceof Error ? reason.message : '读取正文失败');
    } finally {
      if (!pending.signal.aborted) setLoading(false);
    }
  }

  return (
    <section className="content-timeline" aria-label="执行正文">
      <div className="content-heading">
        <div>
          <h3>执行正文</h3>
          <p className="detail-note">
            {turnId ? '当前轮次的归档正文' : '整个会话的归档正文，不随上方统计筛选裁剪'}
            {' · '}按需读取已脱敏记录
          </p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>
          {page || error ? '重新加载正文' : '查看执行正文'}
        </button>
      </div>
      {loading && <p role="status">正在读取归档正文…</p>}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
      {page?.message && (
        <p className="detail-note" role="status">
          {page.message}
        </p>
      )}
      {page?.status === 'available' && (
        <>
          <ol className="content-events">
            {events.map((event) => (
              <EventCard key={event.event_id} sessionId={sessionId} event={event} />
            ))}
          </ol>
          <div className="content-footer">
            <span className="detail-note">已加载 {events.length} 条正文事件</span>
            {page.has_more && (
              <button type="button" disabled={loading} onClick={() => void load(true)}>
                加载更多
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function EventCard({ sessionId, event }: { sessionId: string; event: ContentEvent }) {
  const label = contentLabels[event.kind] || event.kind;
  const collapsible =
    event.kind.startsWith('tool.') ||
    event.kind === 'reasoning.summary' ||
    event.kind === 'context.compacted';
  const toolName = typeof event.data.name === 'string' ? event.data.name : null;
  const heading = (
    <>
      <b>
        {label}
        {toolName ? ` · ${toolName}` : ''}
      </b>
      <time>{formatDate(event.occurred_at)}</time>
    </>
  );
  const body = (
    <>
      {event.kind === 'reasoning.summary' && (
        <p className="detail-note">模型提供的摘要，非完整内部思考或执行事实。</p>
      )}
      {event.kind === 'tool.returned' && (
        <p className="detail-note">已记录工具返回；是否成功以结果内容为准。</p>
      )}
      {typeof event.data.call_id === 'string' && (
        <p className="content-identity">调用 ID：{event.data.call_id}</p>
      )}
      {event.body && Object.keys(event.body).length ? (
        Object.entries(event.body).map(([key, value]) => (
          <div className="content-field" key={key}>
            <h4>{bodyLabels[key] || key}</h4>
            {!isContentReference(value) && (
              <pre className="content-text">{displayContent(value)}</pre>
            )}
            {contentReferences(value, `/${pointerPart(key)}`).map(({ pointer, ref }) => (
              <BlobContent
                key={pointer}
                sessionId={sessionId}
                eventId={event.event_id}
                pointer={pointer}
                reference={ref}
              />
            ))}
          </div>
        ))
      ) : (
        <p className="detail-note">此事件没有可展示的正文。</p>
      )}
      <details className="content-source">
        <summary>来源证据</summary>
        <p>事件 ID：{event.event_id}</p>
        <p>
          归档序号：{event.seq} · 轮次：{event.turn_id || '未归属'}
        </p>
        {event.attribution !== 'explicit' && <p>轮次归属：{event.attribution}</p>}
        {event.source && (
          <p>
            {event.source.path} · 第 {event.source.line} 行
          </p>
        )}
      </details>
    </>
  );
  return (
    <li className={`content-event ${event.kind === 'message.user' ? 'content-user' : ''}`}>
      {collapsible ? (
        <details>
          <summary className="content-event-heading">{heading}</summary>
          <div className="content-event-body">{body}</div>
        </details>
      ) : (
        <>
          <div className="content-event-heading">{heading}</div>
          <div className="content-event-body">{body}</div>
        </>
      )}
    </li>
  );
}

function BlobContent({
  sessionId,
  eventId,
  pointer,
  reference,
}: {
  sessionId: string;
  eventId: string;
  pointer: string;
  reference: ContentReference;
}) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function load() {
    controller.current?.abort();
    const pending = new AbortController();
    controller.current = pending;
    setLoading(true);
    setError(null);
    try {
      const result = await getContentBlob(sessionId, eventId, pointer, pending.signal);
      if (!pending.signal.aborted) setText(result.text);
    } catch (reason) {
      if (!pending.signal.aborted)
        setError(reason instanceof Error ? reason.message : '读取文件失败');
    } finally {
      if (!pending.signal.aborted) setLoading(false);
    }
  }
  return (
    <div className="content-blob">
      <p className="detail-note">
        外置正文 · {formatNumber(reference.bytes)} 字节 · 读取时校验完整性
      </p>
      {text === null ? (
        <button type="button" disabled={loading} onClick={() => void load()}>
          {loading ? '正在读取…' : '加载完整正文'}
        </button>
      ) : (
        <>
          <button type="button" onClick={() => setText(null)}>
            收起正文
          </button>
          <pre className="content-text">{text}</pre>
        </>
      )}
      {error && (
        <p className="notice" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
