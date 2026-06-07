import { PortRow } from './ValueViewer';

function logText(entry: unknown): string {
  if (typeof entry === 'string') return entry;
  if (entry === null) return 'null';
  if (entry === undefined) return 'undefined';
  return JSON.stringify(entry);
}

/** Dense log rows for the trace tab's collapsed logs section. */
export function LogsView({
  logs,
  viewerTitle = 'logs',
}: {
  logs: unknown[];
  viewerTitle?: string;
}) {
  if (logs.length === 0) return null;

  return (
    <div className="trace-dense-list">
      {logs.map((entry, i) => {
        const text = logText(entry);
        return (
          <PortRow
            key={i}
            name={`${i + 1}`}
            value={text}
            viewerTitle={`${viewerTitle} · ${i + 1}`}
            viewerSubtitle="log"
            variant="row"
          />
        );
      })}
    </div>
  );
}
