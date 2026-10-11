import type { AnomalyHit, AnomalyItem, AnomalyList, AnomalySession } from './types';

export const SESSION_CN: Record<AnomalySession, string> = { regular: '', pre: '盘前', post: '盘后', overnight: '夜盘' };

/** Neither researched nor dismissed: what the ⋯ badge and the menu count. */
export function isPending(it: AnomalyItem): boolean {
  return !it.researched && !it.ignored;
}

export function pendingCount(list: AnomalyList | null): number {
  return list ? list.items.filter(isPending).length : 0;
}

export function menuLabel(n: number): string {
  return n > 0 ? `异动 · ${n} 待看` : '异动';
}

export function moveText(h: AnomalyHit): string {
  return `${h.move >= 0 ? '+' : ''}${(h.move * 100).toFixed(1)}%`;
}

/** "10-08 04:20": the server sends Beijing ISO times. */
export function atText(h: AnomalyHit): string {
  return `${h.at.slice(5, 10)} ${h.at.slice(11, 16)}`;
}

/** "10-08 04:20 盘后" (the regular session adds nothing). */
export function hitWhen(h: AnomalyHit): string {
  return SESSION_CN[h.session] ? `${atText(h)} ${SESSION_CN[h.session]}` : atText(h);
}

export function hitText(h: AnomalyHit): string {
  const session = SESSION_CN[h.session] ? ` ${SESSION_CN[h.session]}` : '';
  const volume = h.reason === 'volume' && h.volume_mult !== null ? ` · 放量 ${h.volume_mult.toFixed(1)} 倍` : '';
  return `${moveText(h)}${session} · ${atText(h)} · 平时波动 ${h.mult.toFixed(1)} 倍${volume}`;
}
