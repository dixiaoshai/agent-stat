import type { ReactNode } from 'react';

export function MetricCard({
  label,
  value,
  detail,
  note,
}: {
  label: string;
  value: ReactNode;
  detail: ReactNode;
  note: string;
}) {
  return (
    <article className="metric">
      <h3>{label}</h3>
      <strong>{value}</strong>
      <p>{detail}</p>
      <small>{note}</small>
    </article>
  );
}
