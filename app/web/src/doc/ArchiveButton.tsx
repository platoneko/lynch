import { useState } from 'react';
import { ConfirmSheet, type Confirm } from '../chat/ConfirmSheet';

/** The doc page's 「归档 / 恢复」. Archiving asks first; restoring does not, it only brings the row back. */
export function ArchiveButton({ ticker, archived, onToggle, disabled = false }: {
  ticker: string;
  archived: boolean;
  onToggle: () => Promise<unknown>; // archives or restores; rejects only when that fails
  disabled?: boolean; // offline: the server keeps the archive list
}) {
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState(false);
  const action = archived ? '恢复' : '归档';
  const run = () => {
    setBusy(true);
    onToggle()
      .catch((e: unknown) => setConfirm({
        message: `${action}失败：${e instanceof Error ? e.message : String(e)}`, label: '重试', onConfirm: run,
      }))
      .finally(() => setBusy(false));
  };
  const ask = () => {
    if (archived) run();
    else setConfirm({ message: `归档 ${ticker}？首页默认不再显示它，文档和演算脚本都保留。`, label: '归档', onConfirm: run });
  };
  return (
    <>
      <button className="text-btn" onClick={ask} disabled={busy || disabled}>{action}</button>
      <ConfirmSheet confirm={confirm} onClose={() => setConfirm(null)} />
    </>
  );
}
