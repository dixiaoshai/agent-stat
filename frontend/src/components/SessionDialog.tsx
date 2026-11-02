import { useEffect, useRef, useState } from 'react';
import { getSession } from '../lib/api';
import type { Filters, Selection, SessionDetail, Turn } from '../types';
import { attributionLabel, formatDate, formatNumber, statusLabel } from '../lib/format';

export function SessionDialog({
  selection,
  filters,
  onClose,
}: {
  selection: Selection | null;
  filters: Filters;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (selection && !dialog.open) dialog.showModal();
    if (!selection && dialog.open) dialog.close();
  }, [selection]);

  useEffect(() => {
    if (!selection) {
      setDetail(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setDetail(null);
    getSession(selection.sessionId, filters, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setDetail(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : '读取详情失败');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [selection?.sessionId, filters, selection?.turnId]);

  const close = () => onClose();
  return (
    <dialog
      ref={ref}
      className="detail"
      aria-labelledby="detail-title"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
    >
      <div className="panel-head">
        <div>
          <span className="eyebrow">SESSION DETAIL</span>
          <h2 id="detail-title">{detail?.session.project || '会话详情'}</h2>
          <small className="identity">{detail?.session.id || selection?.sessionId}</small>
        </div>
        <button className="text-button" onClick={close} autoFocus>
          关闭 ×
        </button>
      </div>
      {loading && <p className="empty">正在读取会话证据…</p>}
      {error && (
        <p className="notice" role="alert">
          读取失败：{error}
        </p>
      )}
      {detail && (
        <>
          <p className="metadata">
            来源：{detail.session.source_path || '未知'} · 时区：
            {Intl.DateTimeFormat().resolvedOptions().timeZone}
          </p>
          <p className="metadata">
            查询范围：{filters.start ? formatDate(filters.start) : '不限开始'} →{' '}
            {filters.end ? formatDate(filters.end) : '不限结束'} · 模型 {filters.model || '全部'} ·
            工作区 {filters.project || '全部'}
          </p>
          {detail.warnings.map((warning, i) => (
            <p className="notice" key={`${i}-${warning}`}>
              {warning}
            </p>
          ))}
          {selectedTurns(detail.turns, selection?.turnId).length === 0 && (
            <p className="empty">当前筛选范围没有匹配的轮次。</p>
          )}
          {selectedTurns(detail.turns, selection?.turnId).map((turn) => (
            <TurnDetail key={turn.id} turn={turn} />
          ))}
        </>
      )}
    </dialog>
  );
}

function selectedTurns(turns: Turn[], selectedId?: string) {
  return selectedId ? turns.filter((turn) => turn.id === selectedId) : turns;
}

function TurnDetail({ turn }: { turn: Turn }) {
  const facts: [string, string][] = [
    ['时间', `${formatDate(turn.started_at)} → ${formatDate(turn.ended_at)}`],
    ['模型', turn.model || '未知'],
    ['轮次身份', turn.native_id || '未知'],
    ['归属', attributionLabel(turn.attribution)],
    ['Token', formatNumber(turn.token_total)],
    [
      '输入 / 缓存输入',
      `${formatNumber(turn.input_tokens)} / ${formatNumber(turn.cached_input_tokens)}`,
    ],
    [
      '输出 / 推理输出',
      `${formatNumber(turn.output_tokens)} / ${formatNumber(turn.reasoning_output_tokens)}`,
    ],
    ['费用', '未知'],
    ['模型调用', '未知'],
    ['状态', statusLabel(turn.status)],
  ];
  return (
    <article className="turn">
      <div className="turn-heading">
        <b>轮次 · {turn.native_id}</b>
        <span className="tag">{statusLabel(turn.status)}</span>
      </div>
      <dl className="detail-grid">
        {facts.map(([label, value]) => (
          <DetailFact key={label} label={label} value={value} />
        ))}
      </dl>
      <h3 className="tool-heading">工具调用与事件证据</h3>
      {turn.tools.length ? (
        <div className="detail-table-wrap">
          <table>
            <thead>
              <tr>
                {['工具', '调用 ID', '状态', '输入 / 输出', '归属 / 来源行'].map((x) => (
                  <th key={x}>{x}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {turn.tools.map((tool) => (
                <tr key={tool.call_id}>
                  <td>{tool.name}</td>
                  <td>{tool.call_id}</td>
                  <td>
                    {tool.status === 'result_observed' ? '已观察到结果；是否成功未知' : '未知'}
                  </td>
                  <td>
                    {formatNumber(tool.input_bytes)} / {formatNumber(tool.output_bytes)} 字节
                  </td>
                  <td>
                    {attributionLabel(tool.attribution)} · 第 {tool.line} 行
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="detail-note">没有可归属的工具记录；这不证明完整观测到零调用。</p>
      )}
      <details>
        <summary>事件定位（最多 100 条）</summary>
        {turn.evidence.map((event) => (
          <p key={`${event.line}-${event.timestamp}`}>
            第 {event.line} 行 · {formatDate(event.timestamp)}
          </p>
        ))}
      </details>
      <p className="detail-note">
        费用与模型请求数未核实。字节数不换算为 Token；分类字段不重复相加。
      </p>
    </article>
  );
}

function DetailFact({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}
