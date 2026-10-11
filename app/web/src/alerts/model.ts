/** The grey tag on a done alert row, earnings or anomaly alike. */
export function doneTag(it: { researched: boolean; ignored: boolean }): string | null {
  if (it.researched) return '已调研';
  if (it.ignored) return '已忽略';
  return null;
}
