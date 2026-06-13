import { useState } from 'react';
import { Markdown } from './Markdown';
import { FilePathLink, looksLikePath } from './FilePathLink';

/**
 * Editorial JSON viewer — used for run inputs/outputs/logs/llm_calls/tool_calls.
 *
 * Differences from a raw JSON.stringify dump:
 * - Objects/arrays are collapsible.
 * - String values that look like markdown render as actual markdown.
 * - Multi-line strings render as wrap-preserving prose.
 * - Numbers / bools / null get type-aware styling.
 */

const MD_HINTS =
  // headings, lists, blockquotes, fenced code, bold/italic, links, tables.
  /(^|\n)\s*(#{1,6}\s|[-*+]\s|>\s|\d+\.\s|```)|\*\*[^*]+\*\*|__[^_]+__|\[[^\]]+\]\([^)]+\)|^\|.+\|$/m;

// LaTeX math: $$...$$ display, \(...\) / \[...\] explicit delimiters, or
// $...$ inline whose body contains a backslash/^/_ (avoids currency false
// positives like "I owe $5").
const MATH_HINTS = /\$\$[\s\S]+?\$\$|\\\(|\\\[|\$[^$\n]*[\\^_][^$\n]*\$/;

function looksLikeMarkdown(s: string): boolean {
  if (s.length < 3) return false;
  if (MATH_HINTS.test(s)) return true;
  if (s.length < 12) return false;
  return MD_HINTS.test(s);
}

function StringValue({ value, large = false }: { value: string; large?: boolean }) {
  if (looksLikeMarkdown(value)) {
    return (
      <div
        style={{
          padding: '6px 10px',
          background: 'var(--paper)',
          border: '1px solid var(--rule-2)',
          borderRadius: 3,
          marginTop: 2,
        }}
      >
        <Markdown large={large}>{value}</Markdown>
      </div>
    );
  }
  if (value.includes('\n')) {
    return (
      <pre
        className="mono"
        style={{
          fontSize: large ? 13 : 11,
          color: 'var(--ink-2)',
          margin: '2px 0 0',
          padding: '6px 10px',
          background: 'var(--paper)',
          border: '1px solid var(--rule-2)',
          borderRadius: 3,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          fontFamily: 'var(--mono)',
        }}
      >
        {value}
      </pre>
    );
  }
  if (looksLikePath(value)) {
    return <FilePathLink path={value} className="file-path-link--mono" />;
  }
  return (
    <span
      style={{
        fontFamily: 'var(--serif)',
        fontStyle: 'italic',
        color: 'var(--ink-2)',
        fontSize: large ? 14.5 : 13,
      }}
    >
      {value}
    </span>
  );
}

function PrimitiveBadge({
  text,
  color,
  mono,
  large = false,
}: {
  text: string;
  color: string;
  mono?: boolean;
  large?: boolean;
}) {
  return (
    <span
      className={mono ? 'mono' : 'smallcaps'}
      style={{ color, fontSize: mono ? (large ? 13 : 11.5) : (large ? 10.5 : 9) }}
    >
      {text}
    </span>
  );
}

interface NodeProps {
  value: unknown;
  level: number;
  large?: boolean;
}

function Node({ value, level, large = false }: NodeProps) {
  if (value === null) {
    return <PrimitiveBadge text="null" color="var(--ink-4)" large={large} />;
  }
  if (value === undefined) {
    return <PrimitiveBadge text="undefined" color="var(--ink-4)" large={large} />;
  }
  if (typeof value === 'boolean') {
    return (
      <PrimitiveBadge text={String(value)} color="var(--accent-ink)" large={large} />
    );
  }
  if (typeof value === 'number') {
    return (
      <span
        className="mono"
        style={{ color: 'var(--ink)', fontSize: large ? 13 : 11.5 }}
      >
        {value}
      </span>
    );
  }
  if (typeof value === 'string') {
    return <StringValue value={value} large={large} />;
  }
  if (Array.isArray(value)) {
    return <ArrayNode value={value} level={level} large={large} />;
  }
  if (typeof value === 'object') {
    return <ObjectNode value={value as Record<string, unknown>} level={level} large={large} />;
  }
  return (
    <span className="mono" style={{ color: 'var(--ink-3)' }}>
      {String(value)}
    </span>
  );
}

function Disclosure({
  open,
  onToggle,
  summary,
  large = false,
}: {
  open: boolean;
  onToggle: () => void;
  summary: React.ReactNode;
  large?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      style={{
        background: 'transparent',
        border: 0,
        padding: '2px 0',
        cursor: 'pointer',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        color: 'var(--ink-3)',
        fontFamily: 'var(--sans)',
        fontSize: large ? 12 : 10.5,
      }}
    >
      <span style={{ width: 10, display: 'inline-block', textAlign: 'center', color: 'var(--ink-4)' }}>
        {open ? '▾' : '▸'}
      </span>
      {summary}
    </button>
  );
}

function ArrayNode({ value, level, large = false }: { value: unknown[]; level: number; large?: boolean }) {
  const [open, setOpen] = useState(level < 2);
  if (value.length === 0) {
    return <span style={{ color: 'var(--ink-4)' }}>[ ]</span>;
  }
  return (
    <div>
      <Disclosure
        open={open}
        onToggle={() => setOpen((v) => !v)}
        large={large}
        summary={
          <span className="smallcaps" style={{ fontSize: large ? 10.5 : 9 }}>
            array · {value.length}
          </span>
        }
      />
      {open && (
        <div
          style={{
            paddingLeft: 12,
            borderLeft: '1px solid var(--rule-2)',
            marginLeft: 4,
            marginTop: 2,
          }}
        >
          {value.map((v, i) => (
            <div key={i} style={{ marginTop: 4 }}>
              <span
                className="mono"
                style={{
                  fontSize: large ? 11.5 : 10,
                  color: 'var(--ink-4)',
                  marginRight: 8,
                }}
              >
                [{i}]
              </span>
              <span style={{ display: 'inline-block', verticalAlign: 'top' }}>
                <Node value={v} level={level + 1} large={large} />
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ObjectNode({
  value,
  level,
  large = false,
}: {
  value: Record<string, unknown>;
  level: number;
  large?: boolean;
}) {
  const keys = Object.keys(value);
  const [open, setOpen] = useState(level < 2);
  if (keys.length === 0) {
    return <span style={{ color: 'var(--ink-4)' }}>{'{ }'}</span>;
  }
  return (
    <div>
      <Disclosure
        open={open}
        onToggle={() => setOpen((v) => !v)}
        large={large}
        summary={
          <span className="smallcaps" style={{ fontSize: large ? 10.5 : 9 }}>
            object · {keys.length} {keys.length === 1 ? 'key' : 'keys'}
          </span>
        }
      />
      {open && (
        <div
          style={{
            paddingLeft: 12,
            borderLeft: '1px solid var(--rule-2)',
            marginLeft: 4,
            marginTop: 2,
          }}
        >
          {keys.map((k) => (
            <div key={k} style={{ marginTop: 4 }}>
              <span
                className="mono"
                style={{
                  fontSize: large ? 12.5 : 11,
                  color: 'var(--ink)',
                  marginRight: 8,
                }}
              >
                {k}
              </span>
              <span style={{ display: 'inline-block', verticalAlign: 'top', maxWidth: '100%' }}>
                <Node value={value[k]} level={level + 1} large={large} />
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function isPrimitiveValue(v: unknown): boolean {
  return v === null || v === undefined || ['string', 'number', 'boolean'].includes(typeof v);
}

function isPrimitiveArray(arr: unknown[]): boolean {
  return arr.every(isPrimitiveValue);
}

function isObjectRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function formatPrimitive(v: unknown): string {
  if (v === null) return 'null';
  if (v === undefined) return 'undefined';
  return String(v);
}

function PanelPrimitive({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="viewer-primitive viewer-primitive--null">{formatPrimitive(value)}</span>;
  }
  if (typeof value === 'boolean') {
    return <span className="viewer-primitive viewer-primitive--bool">{String(value)}</span>;
  }
  if (typeof value === 'number') {
    return <span className="viewer-primitive viewer-primitive--num">{value}</span>;
  }
  return <span className="viewer-primitive">{formatPrimitive(value)}</span>;
}

function PanelString({ value }: { value: string }) {
  if (looksLikeMarkdown(value)) {
    return <Markdown large>{value}</Markdown>;
  }
  if (value.includes('\n')) {
    return <pre className="viewer-block mono">{value}</pre>;
  }
  if (looksLikePath(value)) {
    return <FilePathLink path={value} className="file-path-link--mono" />;
  }
  return <span className="viewer-primitive">{value}</span>;
}

function PanelPrimitiveList({ items, compact = false }: { items: unknown[]; compact?: boolean }) {
  if (compact) {
    return (
      <div className="viewer-tags">
        {items.map((item, i) =>
          looksLikePath(item) ? (
            <FilePathLink key={i} path={item} className="file-path-link--mono" />
          ) : (
            <span key={i} className="viewer-tag">{formatPrimitive(item)}</span>
          ),
        )}
      </div>
    );
  }
  return (
    <div className="viewer-data">
      <div className="viewer-data__meta">
        {items.length} {items.length === 1 ? 'item' : 'items'}
      </div>
      <div className="viewer-list">
        {items.map((item, i) => (
          <div key={i} className="viewer-list__row">
            <span className="viewer-list__idx">[{i}]</span>
            <span className="viewer-list__val">
              {looksLikePath(item) ? (
                <FilePathLink path={item} className="file-path-link--mono" />
              ) : (
                formatPrimitive(item)
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function PanelField({ name, value }: { name: string; value: unknown }) {
  return (
    <div className="viewer-field">
      <div className="viewer-field__key">{name}</div>
      <div className="viewer-field__val">
        <PanelValue value={value} nested />
      </div>
    </div>
  );
}

function PanelObject({
  value,
  title,
  nested = false,
}: {
  value: Record<string, unknown>;
  title?: string;
  nested?: boolean;
}) {
  const keys = Object.keys(value);
  if (keys.length === 0) {
    return <div className="viewer-scalar">{'{ }'}</div>;
  }
  return (
    <div className={`viewer-record${nested ? ' viewer-record--nested' : ''}`}>
      {title && <div className="viewer-record__head">{title}</div>}
      <div className="viewer-record__fields">
        {keys.map((k) => (
          <PanelField key={k} name={k} value={value[k]} />
        ))}
      </div>
    </div>
  );
}

function PanelArray({ value, nested = false }: { value: unknown[]; nested?: boolean }) {
  if (value.length === 0) {
    return <div className="viewer-scalar">[ ]</div>;
  }
  if (isPrimitiveArray(value)) {
    if (nested && value.length <= 8) {
      return <PanelPrimitiveList items={value} compact />;
    }
    return <PanelPrimitiveList items={value} />;
  }

  const objectItems = value.filter(isObjectRecord);
  const allObjects = objectItems.length === value.length;

  if (allObjects) {
    const body = (
      <div className="viewer-collection">
        {value.map((item, i) => (
          <PanelObject
            key={i}
            value={item as Record<string, unknown>}
            title={`[${i}]`}
            nested
          />
        ))}
      </div>
    );
    if (nested) return body;
    return (
      <div className="viewer-data">
        <div className="viewer-data__meta">
          {value.length} {value.length === 1 ? 'record' : 'records'}
        </div>
        {body}
      </div>
    );
  }

  const body = (
    <div className="viewer-collection">
      {value.map((item, i) => (
        <div key={i} className="viewer-array-item">
          <div className="viewer-array-item__head">[{i}]</div>
          <div className="viewer-array-item__body">
            <PanelValue value={item} nested />
          </div>
        </div>
      ))}
    </div>
  );
  if (nested) return body;
  return (
    <div className="viewer-data">
      <div className="viewer-data__meta">
        {value.length} {value.length === 1 ? 'item' : 'items'}
      </div>
      {body}
    </div>
  );
}

function PanelValue({ value, nested = false }: { value: unknown; nested?: boolean }) {
  // Path strings get the clickable file affordance before the generic
  // primitive renderer claims them.
  if (looksLikePath(value)) {
    return <FilePathLink path={value} className="file-path-link--mono" />;
  }
  if (isPrimitiveValue(value)) {
    return <PanelPrimitive value={value} />;
  }
  if (typeof value === 'string') {
    return <PanelString value={value} />;
  }
  if (Array.isArray(value)) {
    return <PanelArray value={value} nested={nested} />;
  }
  if (isObjectRecord(value)) {
    return <PanelObject value={value} nested={nested} />;
  }
  return <span className="viewer-primitive">{String(value)}</span>;
}

function PanelDataView({ value }: { value: unknown }) {
  if (isPrimitiveValue(value)) {
    return <div className="viewer-scalar">{formatPrimitive(value)}</div>;
  }
  if (typeof value === 'string') {
    return (
      <div className="viewer-data">
        <PanelString value={value} />
      </div>
    );
  }
  if (Array.isArray(value)) {
    return <PanelArray value={value} />;
  }
  if (isObjectRecord(value)) {
    const keys = Object.keys(value);
    return (
      <div className="viewer-data">
        {keys.length > 0 && (
          <div className="viewer-data__meta">
            {keys.length} {keys.length === 1 ? 'field' : 'fields'}
          </div>
        )}
        <PanelObject value={value} />
      </div>
    );
  }
  return <div className="viewer-scalar">{String(value)}</div>;
}

export function JsonView({ value, large = false }: { value: unknown; large?: boolean }) {
  if (large) {
    return <PanelDataView value={value} />;
  }
  // Compact inline card for run-trace rows.
  return (
    <div
      style={{
        padding: 8,
        background: 'var(--paper)',
        border: '1px solid var(--rule-2)',
        borderRadius: 3,
        fontSize: 12,
        color: 'var(--ink-2)',
        maxWidth: '100%',
        overflowX: 'auto',
      }}
    >
      <Node value={value} level={0} large={false} />
    </div>
  );
}
