/* 内联 SVG 图标。不引图标库：12 个图标手写 ~2KB，且能保证描边粗细统一。 */
const P = {
  chevron:  'M4 6l4 4 4-4',
  chevronUp:'M4 10l4-4 4 4',
  check:    'M3.5 8.5l3 3 6-6.5',
  x:        'M4 4l8 8M12 4l-8 8',
  upload:   'M8 11V3M5 6l3-3 3 3M3 11v1.5A1.5 1.5 0 004.5 14h7a1.5 1.5 0 001.5-1.5V11',
  download: 'M8 3v8M5 8l3 3 3-3M3 12v.5A1.5 1.5 0 004.5 14h7a1.5 1.5 0 001.5-1.5V12',
  search:   'M11.5 11.5L14 14M7.2 12.4a5.2 5.2 0 100-10.4 5.2 5.2 0 000 10.4z',
  file:     'M9 2H5a1.5 1.5 0 00-1.5 1.5v9A1.5 1.5 0 005 14h6a1.5 1.5 0 001.5-1.5V5.5L9 2zM9 2v3.5h3.5',
  sheet:    'M3 3h10v10H3zM3 6.5h10M6.5 6.5V13',
  alert:    'M8 5.5v3.5M8 11.2v.1M8 2l6 11H2L8 2z',
  arrow:    'M3 8h9M9 5l3 3-3 3',
  clock:    'M8 4.5V8l2.2 1.3M14 8A6 6 0 112 8a6 6 0 0112 0z',
  layers:   'M8 2l6 3-6 3-6-3 6-3zM2 11l6 3 6-3M2 8l6 3 6-3',
  history:  'M3 8a5 5 0 105-5 5 5 0 00-4.3 2.4M3 3v2.6h2.6M8 5.5V8l2 1.2',
};

export default function Icon({ name, size = 14, className = '', style }) {
  const d = P[name];
  if (!d) return null;
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      className={className} style={{ flex: 'none', ...style }} aria-hidden="true">
      <path d={d} />
    </svg>
  );
}
