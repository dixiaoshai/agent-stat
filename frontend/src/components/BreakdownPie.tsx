export interface BreakdownSegment {
  label: string;
  value: number;
  color: string;
}

export function BreakdownPie({
  segments,
  label,
  className,
}: {
  segments: BreakdownSegment[];
  label: string;
  className?: string;
}) {
  const visible = segments.filter((segment) => Number.isFinite(segment.value) && segment.value > 0);
  const total = visible.reduce((sum, segment) => sum + segment.value, 0);
  let angle = -Math.PI / 2;
  return (
    <svg className={className} viewBox="0 0 120 120" role="img" aria-label={label}>
      <circle cx="60" cy="60" r="60" fill="#ded9cf" />
      {total > 0 &&
        (visible.length === 1 ? (
          <circle cx="60" cy="60" r="60" fill={visible[0].color} />
        ) : (
          visible.map((segment) => {
            const start = angle;
            const portion = segment.value / total;
            angle += portion * 2 * Math.PI;
            const startPoint = `${60 + 60 * Math.cos(start)},${60 + 60 * Math.sin(start)}`;
            const endPoint = `${60 + 60 * Math.cos(angle)},${60 + 60 * Math.sin(angle)}`;
            return (
              <path
                key={segment.label}
                d={`M60,60 L${startPoint} A60,60 0 ${portion > 0.5 ? 1 : 0},1 ${endPoint} Z`}
                fill={segment.color}
              />
            );
          })
        ))}
    </svg>
  );
}
