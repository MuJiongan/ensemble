type CloseButtonProps = {
  onClick: () => void;
  title?: string;
  className?: string;
  style?: React.CSSProperties;
};

function CloseIcon() {
  return (
    <svg className="close-btn__icon" width="14" height="14" viewBox="0 0 14 14" aria-hidden>
      <path
        d="M3.25 3.25l7.5 7.5M10.75 3.25l-7.5 7.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function CloseButton({
  onClick,
  title = 'close',
  className,
  style,
}: CloseButtonProps) {
  return (
    <button
      type="button"
      className={`close-btn${className ? ` ${className}` : ''}`}
      onClick={onClick}
      title={title}
      aria-label={title}
      style={style}
    >
      <CloseIcon />
    </button>
  );
}
