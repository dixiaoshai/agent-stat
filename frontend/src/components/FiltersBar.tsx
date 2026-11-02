export interface FilterControls {
  range: string;
  project: string;
  model: string;
  start: string;
  end: string;
}

export function FiltersBar({
  value,
  projects,
  models,
  onChange,
}: {
  value: FilterControls;
  projects: string[];
  models: string[];
  onChange: (value: FilterControls) => void;
}) {
  const update = (key: keyof FilterControls, next: string) => onChange({ ...value, [key]: next });
  return (
    <section className="filters" aria-label="全局筛选">
      <label>
        时间{' '}
        <select value={value.range} onChange={(e) => update('range', e.target.value)}>
          <option value="7">近 7 天</option>
          <option value="30">近 30 天</option>
          <option value="90">近 90 天</option>
          <option value="all">全部时间</option>
          <option value="custom">自定义</option>
        </select>
      </label>
      <label>
        Agent{' '}
        <select aria-label="Agent" disabled>
          <option>Codex</option>
        </select>
      </label>
      <label>
        模型{' '}
        <select value={value.model} onChange={(e) => update('model', e.target.value)}>
          <option value="">全部模型</option>
          {models.map((model) => (
            <option key={model}>{model}</option>
          ))}
        </select>
      </label>
      <label>
        工作区{' '}
        <select value={value.project} onChange={(e) => update('project', e.target.value)}>
          <option value="">全部工作区</option>
          {projects.map((project) => (
            <option key={project}>{project}</option>
          ))}
        </select>
      </label>
      {value.range === 'custom' && (
        <span id="custom-dates">
          <label>
            开始{' '}
            <input
              type="datetime-local"
              value={value.start}
              onChange={(e) => update('start', e.target.value)}
            />
          </label>
          <label>
            结束（不含）{' '}
            <input
              type="datetime-local"
              value={value.end}
              onChange={(e) => update('end', e.target.value)}
            />
          </label>
        </span>
      )}
    </section>
  );
}
