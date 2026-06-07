import { useEffect, useState } from 'react';

type SecretInputProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'> & {
  /** When false, shows plain text (e.g. reveal-keys toggle). */
  masked?: boolean;
};

/** API key / token field — masks like a password without triggering browser
 *  password-manager UI (save key icon, autofill prompts). */
export function SecretInput({
  masked = true,
  className,
  onFocus,
  readOnly,
  style,
  ...props
}: SecretInputProps) {
  const [armed, setArmed] = useState(!masked);

  useEffect(() => {
    setArmed(!masked);
  }, [masked]);

  const handleFocus = (e: React.FocusEvent<HTMLInputElement>) => {
    if (masked && !armed) setArmed(true);
    onFocus?.(e);
  };

  const input = (
    <input
      type="search"
      className={`field field--mono${masked ? ' field--secret' : ''}${className ? ` ${className}` : ''}`}
      autoComplete="off"
      autoCorrect="off"
      autoCapitalize="off"
      data-1p-ignore="true"
      data-lpignore="true"
      data-bwignore="true"
      data-protonpass-ignore="true"
      data-form-type="other"
      spellCheck={false}
      readOnly={readOnly ?? (masked && !armed)}
      onFocus={handleFocus}
      style={style}
      {...props}
    />
  );

  if (!masked) return input;

  return <div className="field-secret-wrap">{input}</div>;
}
