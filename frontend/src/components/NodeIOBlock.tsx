import type { IOPort, WorkflowDetail } from '../types';
import { IOSection, type IOFieldEntry } from './IOSection';

function buildPortEntries({
  values,
  schema,
  workflow,
  nodeId,
  nodeName,
  kind,
}: {
  values: Record<string, unknown>;
  schema: IOPort[];
  workflow: WorkflowDetail;
  nodeId: string;
  nodeName: string;
  kind: 'inputs' | 'outputs';
}): IOFieldEntry[] {
  const seen = new Set<string>();
  const entries: IOFieldEntry[] = [];
  const ioLabel = kind === 'inputs' ? 'in' : 'out';

  for (const port of schema) {
    seen.add(port.name);
    if (!(port.name in values)) continue;
    let viewerSubtitle: string | undefined;
    if (kind === 'inputs') {
      const e = workflow.edges.find(
        (x) => x.to_node_id === nodeId && x.to_input === port.name,
      );
      if (e) {
        const src = workflow.nodes.find((n) => n.id === e.from_node_id);
        viewerSubtitle = `from ${src?.name ?? e.from_node_id}.${e.from_output}`;
      }
    }
    entries.push({
      name: port.name,
      value: values[port.name],
      typeHint: port.type_hint,
      viewerTitle: `${nodeName} · ${ioLabel} · ${port.name}`,
      viewerSubtitle,
    });
  }

  for (const k of Object.keys(values)) {
    if (seen.has(k)) continue;
    entries.push({
      name: k,
      value: values[k],
      viewerTitle: `${nodeName} · ${ioLabel} · ${k}`,
    });
  }

  return entries;
}

export function NodeIOBlock({
  workflow,
  nodeId,
  nodeName,
  inputs,
  outputs,
}: {
  workflow: WorkflowDetail;
  nodeId: string;
  nodeName: string;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
}) {
  const schemaNode = workflow.nodes.find((n) => n.id === nodeId);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {inputs !== undefined && (
        <IOSection
          title="inputs"
          emptyText="this node received no inputs."
          accent="input"
          entries={buildPortEntries({
            values: inputs,
            schema: schemaNode?.inputs ?? [],
            workflow,
            nodeId,
            nodeName,
            kind: 'inputs',
          })}
        />
      )}
      {outputs !== undefined && (
        <IOSection
          title="outputs"
          emptyText="no outputs recorded for this node."
          accent="output"
          entries={buildPortEntries({
            values: outputs,
            schema: schemaNode?.outputs ?? [],
            workflow,
            nodeId,
            nodeName,
            kind: 'outputs',
          })}
        />
      )}
    </div>
  );
}
