import { createPortal } from 'react-dom';

/** A question awaiting 取消 / `label`; `onConfirm` runs only on the latter. */
export type Confirm = { message: string; label: string; onConfirm: () => void };

/**
 * In-page stand-in for the browser confirm dialog, which a WebView without a chrome client answers
 * with a silent "cancel". Portaled to the body so it stacks above any other sheet.
 */
export function ConfirmSheet({ confirm, onClose }: { confirm: Confirm | null; onClose: () => void }) {
  if (!confirm) return null;
  const accept = () => {
    onClose();
    confirm.onConfirm();
  };
  return createPortal(
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" onClick={(e) => e.stopPropagation()}>
        <p>{confirm.message}</p>
        <div className="sheet-actions">
          <button className="text-btn" onClick={onClose}>取消</button>
          <button className="text-btn" onClick={accept}>{confirm.label}</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
