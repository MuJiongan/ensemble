import { formatTokenCount, type ModelStat } from '../appHelpers';

function ModelSegment({ stat }: { stat: ModelStat }) {
  const parts: string[] = [`${stat.calls} call${stat.calls === 1 ? '' : 's'}`];
  if (stat.promptTokens > 0 || stat.completionTokens > 0) {
    parts.push(`${formatTokenCount(stat.promptTokens)} in`);
    parts.push(`${formatTokenCount(stat.completionTokens)} out`);
  }
  if (stat.cost > 0) parts.push(`$${stat.cost.toFixed(4)}`);

  return (
    <span className="execution-stats__segment">
      <span className="execution-stats__model">{stat.model}</span>
      {parts.map((p) => (
        <span key={p} className="execution-stats__metric">{p}</span>
      ))}
    </span>
  );
}

/** Unified LLM + tool usage byline for run and node trace headers. */
export function ExecutionStats({
  modelStats,
  toolCalls,
  marginTop = 8,
}: {
  modelStats?: ModelStat[] | null;
  toolCalls?: number | null;
  marginTop?: number;
}) {
  const hasModels = !!modelStats?.length;
  const hasTools = !!toolCalls && toolCalls > 0;
  if (!hasModels && !hasTools) return null;

  return (
    <div className="execution-stats" style={{ marginTop }}>
      {hasModels &&
        modelStats!.map((s, i) => (
          <span key={s.model} className="execution-stats__wrap">
            {i > 0 && <span className="execution-stats__between" aria-hidden>·</span>}
            <ModelSegment stat={s} />
          </span>
        ))}
      {hasModels && hasTools && (
        <span className="execution-stats__rail" aria-hidden />
      )}
      {hasTools && (
        <span className="execution-stats__segment">
          <span className="execution-stats__metric">
            {toolCalls} tool call{toolCalls === 1 ? '' : 's'}
          </span>
        </span>
      )}
    </div>
  );
}
