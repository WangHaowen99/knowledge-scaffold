// 交互稿中的线性图标（24×24，描边由 CSS 控制），保持与设计一致
const PATHS = {
  flywheel: (
    <>
      <path d="M3.5 10A8.5 8.5 0 0 1 18 5.5L20.5 8M20.5 8V3.5M20.5 8H16M20.5 14A8.5 8.5 0 0 1 6 18.5l-2.5-2.5M3.5 16v4.5M3.5 16H8" />
      <path d="m12 7 1.3 3.7L17 12l-3.7 1.3L12 17l-1.3-3.7L7 12l3.7-1.3L12 7Z" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  'circle-check': <><circle cx="12" cy="12" r="9" /><path d="m8 12 3 3 5-6" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7h.01" /></>,
  warning: <><path d="m10.3 4.4-8 14A1.8 1.8 0 0 0 4 21h16a1.8 1.8 0 0 0 1.7-2.6l-8-14a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4M12 17h.01" /></>,
  close: <path d="m6 6 12 12M6 18 18 6" />,
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  chevron: <path d="m8 5 7 7-7 7" />,
  down: <path d="m6 9 6 6 6-6" />,
  package: <><path d="m12 2 9 5v10l-9 5-9-5V7l9-5Z" /><path d="m3 7 9 5 9-5M12 12v10M7.5 4.5l9 5" /></>,
  shield: <><path d="m12 2 8 3v6c0 5-8 10-8 10S4 16 4 11V5l8-3Z" /><path d="m8 11 3 3 5-5" /></>,
  history: <path d="M3 11a9 9 0 1 1 2.6 7.4M3 4v7h7M12 7v5l4 2" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  file: <path d="M14 2H5v20h14V7l-5-5ZM14 2v6h5M8 12h8M8 16h6" />,
  rollback: <path d="m9 4-6 6 6 6M3 10h11a7 7 0 0 1 0 14" transform="translate(0 -2)" />,
  // 齿轮：8 个齿的外轮廓加中心圆，用于「设置」
  gear: (
    <>
      <path d="M9.99 4.98L10.33 2.55L13.67 2.55L14.01 4.98L15.54 5.62L17.51 4.14L19.86 6.49L18.38 8.46L19.02 9.99L21.45 10.33L21.45 13.67L19.02 14.01L18.38 15.54L19.86 17.51L17.51 19.86L15.54 18.38L14.01 19.02L13.67 21.45L10.33 21.45L9.99 19.02L8.46 18.38L6.49 19.86L4.14 17.51L5.62 15.54L4.98 14.01L2.55 13.67L2.55 10.33L4.98 9.99L5.62 8.46L4.14 6.49L6.49 4.14L8.46 5.62Z" strokeLinejoin="round" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  bell: <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />,
  hub: <><rect x="8" y="8" width="8" height="8" rx="2" /><path d="M12 2v6M12 16v6M2 12h6M16 12h6M5 5l4 4M15 15l4 4M19 5l-4 4M9 15l-4 4" /></>,
};

export default function Icon({ name, size = '', className = '' }) {
  return (
    <svg className={`fw-icon ${size} ${className}`.trim()} viewBox="0 0 24 24" aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}
