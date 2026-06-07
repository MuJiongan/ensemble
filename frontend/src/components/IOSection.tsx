import { PortRow } from './ValueViewer';

export interface IOFieldEntry {
  name: string;
  value: unknown;
  viewerTitle: string;
  viewerSubtitle?: string;
  typeHint?: string;
}

/** Raised field cards for run/node inputs and outputs — shared by the run
 * details panel and per-node trace tab. */
export function IOSection({
  title,
  emptyText,
  entries,
  accent,
}: {
  title: string;
  emptyText: string;
  entries: IOFieldEntry[];
  accent: 'input' | 'output';
}) {
  return (
    <section className="snapshot-io-section">
      <div className="snapshot-io-section__head">
        <span className="smallcaps snapshot-io-section__title">{title}</span>
        {entries.length > 0 && (
          <span className="snapshot-io-section__count">
            {entries.length} {entries.length === 1 ? 'field' : 'fields'}
          </span>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="snapshot-io-section__empty">{emptyText}</div>
      ) : (
        <div className="snapshot-io-fields">
          {entries.map((e) => (
            <PortRow
              key={e.name}
              name={e.name}
              typeHint={e.typeHint}
              value={e.value}
              viewerTitle={e.viewerTitle}
              viewerSubtitle={e.viewerSubtitle}
              variant="card"
              cardAccent={accent}
            />
          ))}
        </div>
      )}
    </section>
  );
}
