export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <img
      className="brand-mark"
      src="/agents/01-wave.png"
      alt=""
      width={size}
      height={size}
      draggable={false}
    />
  );
}
