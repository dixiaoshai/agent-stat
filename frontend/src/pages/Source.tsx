import { useEffect, useState } from 'react';
import type { Capabilities } from '../types';
import { getCapabilities } from '../lib/api';

export function Source() {
  const [data, setData] = useState<Capabilities | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getCapabilities(controller.signal)
      .then(setData)
      .catch((e: Error) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, []);
  return (
    <>
      <h2 className="section-label">来源与覆盖</h2>
      <p className="muted">统计读取本机 Codex 会话记录，能力按已核实字段说明。缺失信息保持未知。</p>
      <article className="panel source-panel">
        {error ? (
          <p role="alert">来源能力加载失败：{error}</p>
        ) : !data ? (
          <p>正在读取来源能力…</p>
        ) : (
          <>
            <h3>
              {data.source} · 适配器 {data.adapter_version}
            </h3>
            <p className="metadata">{data.mode}</p>
            {data.items.map((item) => (
              <div className="cap" key={item.name}>
                <b>{item.name}</b>
                <span className="tag">{item.status}</span>
                <p>{item.detail}</p>
              </div>
            ))}
            <p className="metadata">详细接入依据：{data.documentation}</p>
          </>
        )}
      </article>
      <article className="panel privacy">
        <h3>本机数据处理</h3>
        <p>
          服务仅在本机运行。保存会话名称、最多 80 字符的 Prompt
          轮次摘要与统计元数据，不保存完整提示词、命令、思考或工具输出正文。摘要、名称、路径与事件身份仍可能属于敏感信息。
        </p>
      </article>
    </>
  );
}
