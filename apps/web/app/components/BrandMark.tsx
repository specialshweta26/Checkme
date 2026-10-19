export function BrandMark({ size = 28 }: { size?: number }) {
  const id = `spark-${size}`;
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden className="brand-mark">
      <defs>
        <linearGradient id={id} x1="6" y1="3" x2="26" y2="29">
          <stop stopColor="#6B94FF" />
          <stop offset="1" stopColor="#2F6BFF" />
        </linearGradient>
      </defs>
      <path
        d="M16 1.8 19.6 12.4 30.2 16 19.6 19.6 16 30.2 12.4 19.6 1.8 16 12.4 12.4Z"
        fill={`url(#${id})`}
      />
    </svg>
  );
}
