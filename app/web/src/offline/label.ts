const pad = (n: number) => String(n).padStart(2, '0');

/** The offline marker: when the home list on screen was saved, so its prices are not read as current. */
export function offlineLabel(savedAt: string | null): string {
  if (savedAt === null) return '离线';
  const d = new Date(savedAt);
  return `离线 · 数据存于 ${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
