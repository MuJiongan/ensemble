import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

/**
 * Renders markdown using the editorial palette: serif body for prose, mono
 * for code, sage for accents/links. Used wherever an LLM-produced text
 * value lands in the UI (mostly inside JsonView for run inputs/outputs).
 */
export function Markdown({ children, large = false }: { children: string; large?: boolean }) {
  return (
    <div
      style={{
        fontFamily: 'var(--serif)',
        fontSize: large ? 15.5 : 13,
        lineHeight: large ? 1.68 : 1.6,
        color: 'var(--ink-2)',
      }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
        rehypePlugins={[rehypeKatex]}
        components={{
          p: ({ children }) => <p style={{ margin: large ? '0 0 12px' : '0 0 8px' }}>{children}</p>,
          h1: ({ children }) => (
            <h1
              className="serif"
              style={{
                fontSize: large ? 24 : 18,
                fontWeight: 500,
                margin: large ? '0 0 14px' : '6px 0 6px',
                color: 'var(--ink)',
                lineHeight: 1.25,
              }}
            >
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2
              className="serif"
              style={{
                fontSize: large ? 19 : 16,
                fontWeight: 500,
                margin: large ? '20px 0 10px' : '6px 0 6px',
                color: 'var(--ink)',
                lineHeight: 1.3,
              }}
            >
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3
              className="serif"
              style={{ fontSize: large ? 16 : 14, fontWeight: 500, margin: '4px 0 4px', color: 'var(--ink)' }}
            >
              {children}
            </h3>
          ),
          h4: ({ children }) => (
            <h4
              className="smallcaps"
              style={{ margin: '4px 0 4px', color: 'var(--ink-2)' }}
            >
              {children}
            </h4>
          ),
          em: ({ children }) => (
            <em style={{ fontStyle: 'italic', color: 'var(--ink)' }}>{children}</em>
          ),
          strong: ({ children }) => (
            <strong style={{ color: 'var(--ink)', fontWeight: 600 }}>{children}</strong>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: 'var(--accent-ink)', textDecoration: 'underline' }}
            >
              {children}
            </a>
          ),
          ul: ({ children }) => (
            <ul
              style={{
                paddingLeft: 22,
                margin: '0 0 8px',
                listStyleType: 'disc',
                listStylePosition: 'outside',
              }}
            >
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol
              style={{
                paddingLeft: 22,
                margin: '0 0 8px',
                listStyleType: 'decimal',
                listStylePosition: 'outside',
              }}
            >
              {children}
            </ol>
          ),
          li: ({ children }) => (
            <li style={{ marginBottom: 2, paddingLeft: 2 }}>{children}</li>
          ),
          blockquote: ({ children }) => (
            <blockquote
              style={{
                borderLeft: '2px solid var(--rule)',
                paddingLeft: 10,
                color: 'var(--ink-3)',
                margin: '0 0 8px',
                fontStyle: 'italic',
              }}
            >
              {children}
            </blockquote>
          ),
          hr: () => (
            <hr
              style={{
                border: 0,
                borderTop: '1px solid var(--rule)',
                margin: large ? '24px 0' : '12px 0',
              }}
            />
          ),
          code: ({ inline, children, className }: any) => {
            if (inline) {
              return (
                <code
                  className="mono"
                  style={{
                    background: 'var(--surface-chip)',
                    padding: '0 4px',
                    borderRadius: 2,
                    fontSize: '0.92em',
                    color: 'var(--ink)',
                  }}
                >
                  {children}
                </code>
              );
            }
            return (
              <code className={`mono ${className ?? ''}`} style={{ fontSize: large ? 13 : 11.5 }}>
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            <pre
              className="scroll"
              style={{
                background: 'var(--surface-chip)',
                border: '1px solid var(--rule-2)',
                borderRadius: 3,
                padding: 10,
                overflow: 'auto',
                margin: '0 0 8px',
                fontSize: large ? 13 : 11.5,
                lineHeight: 1.55,
                color: 'var(--ink-2)',
                whiteSpace: 'pre',
              }}
            >
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div style={{ overflowX: 'auto', margin: '0 0 8px' }}>
              <table
                style={{
                  borderCollapse: 'collapse',
                  fontSize: large ? 13.5 : 12,
                  fontFamily: 'var(--sans)',
                }}
              >
                {children}
              </table>
            </div>
          ),
          th: ({ children }) => (
            <th
              className="smallcaps"
              style={{
                textAlign: 'left',
                padding: '4px 8px',
                borderBottom: '1px solid var(--rule)',
              }}
            >
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td
              style={{
                padding: '4px 8px',
                borderBottom: '1px solid var(--rule-2)',
                color: 'var(--ink-2)',
              }}
            >
              {children}
            </td>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
