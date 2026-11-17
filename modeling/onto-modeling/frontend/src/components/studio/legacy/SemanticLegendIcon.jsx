// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import { semanticIconRoleColors } from './webgl/semantic-icon-colors.js';

const gearOutline = Array.from({ length: 6 }, (_, tooth) =>
  [[-0.5, 0.65], [-0.34, 0.65], [-0.24, 0.87], [0.24, 0.87], [0.34, 0.65]].map(([offset, radius]) => {
    const angle = ((tooth + offset) / 6) * Math.PI * 2 + Math.PI / 2;
    return `${Math.cos(angle) * radius},${Math.sin(angle) * radius}`;
  }).join(' '),
).join(' ');

// Front-view silhouettes of the semantic 3D icons, without loading a renderer.
export default function SemanticLegendIcon({ kind, className, color, mode = 'light' }) {
  const roles = semanticIconRoleColors(kind, mode);
  return (
    <svg className={className} viewBox="-1 -1 2 2" width="22" height="22"
      style={{ color }} fill="currentColor" aria-hidden="true" focusable="false" data-semantic-kind={kind}>
      <g transform="scale(1 -1)" strokeLinecap="round" strokeLinejoin="round" fill={roles.body}>
        {kind === 'object' && <>
          <path d="M-.73 .4 0 .04 .73 .4 M0 .04V-.73" fill="none" stroke={roles.body} strokeWidth=".13" />
          <circle cx="-.73" cy=".4" r=".145" fill={roles.light} data-icon-role="light" />
          <circle cx=".73" cy=".4" r=".145" fill={roles.light} />
          <circle cx="0" cy="-.73" r=".145" fill={roles.light} />
          <circle cy=".04" r=".5" fill={roles.body} data-icon-role="body" />
          <path d="M-.3 .14A.32 .32 0 0 0-.08 .35" fill="none" stroke={roles.light} strokeWidth=".1" />
        </>}
        {kind === 'operation' && <>
          <polygon points={gearOutline} fill={roles.body} data-icon-role="body" />
          <path d="M-.45-.12H.06V-.32L.46 0 .06 .32V.12H-.45Z" fill={roles.dark} data-icon-role="dark" />
        </>}
        {kind === 'rule' && <>
          <path d="M-.1-.22C-.2-.6-.62-.37-.62-.79 M.1-.22C.2-.6.62-.37.62-.79"
            fill="none" stroke={roles.body} strokeWidth=".13" />
          <circle cx="-.62" cy="-.79" r=".155" fill={roles.light} data-icon-role="light" />
          <circle cx=".62" cy="-.79" r=".155" fill={roles.light} />
          <path d="M0 .93.64 .3 0-.33-.64 .3Z" fill={roles.body} data-icon-role="body" />
        </>}
        {kind === 'script' && <>
          <path d="M-.65-.77H.65V.41L.28 .8H-.65Z" fill={roles.body} data-icon-role="body" />
          <path d="M.28 .77V.42H.61Z" fill={roles.light} data-icon-role="light" />
          <path d="M-.27 .18-.47-.04-.27-.26 M-.1-.32.09 .28 M.27 .18.47-.04.27-.26"
            fill="none" stroke={roles.dark} strokeWidth=".1" data-icon-role="dark" />
        </>}
        {kind === 'metric' && <g transform="translate(0 .11)">
          <path d="M-.88-.45A.88 .88 0 0 0 .88-.45Z" fill={roles.inset} data-icon-role="inset" />
          <path d="M-.785-.45A.785 .785 0 0 0 .785-.45" fill="none" stroke={roles.body} strokeWidth=".19" data-icon-role="body" />
          <path d="M-.54-.11-.45-.17 M0 .19V.08 M.54-.11.45-.17 M0-.45.48 .03"
            fill="none" stroke={roles.light} strokeWidth=".09" />
          <circle cy="-.45" r=".135" fill={roles.light} data-icon-role="light" />
        </g>}
      </g>
    </svg>
  );
}
