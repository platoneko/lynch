export type MenuItem = { label: string; onSelect: () => void; disabled?: boolean };

/** The "⋯" dropdown: low-traffic page actions. Picking an item or tapping outside closes it.
 * It hangs from the nearest positioned ancestor: the page's sticky header, or the row holding its button. */
export function Menu({ items, onClose }: { items: MenuItem[]; onClose: () => void }) {
  return (
    <>
      <div className="menu-backdrop" onClick={onClose} />
      <div className="menu">
        {items.map((it) => (
          <button key={it.label} disabled={it.disabled} onClick={() => { onClose(); it.onSelect(); }}>{it.label}</button>
        ))}
      </div>
    </>
  );
}
