import { useState } from 'react';
import { saveWatch } from './api';
import { replacesText, sameCard } from './model';
import type { WatchCard, WatchItem } from './types';

/** The watch card one 讨论 reply ends with. Ticking and tapping 加入观察区 is the user's consent to save it,
 * replacing the card the ticker already has. */
export function WatchCardView({ convId, itemKey, card, saved, onSaved }: {
  convId: string;
  itemKey: string;
  card: WatchCard;
  saved: WatchItem | null; // the ticker's card on the list now
  onSaved: () => void;
}) {
  const already = saved !== null && sameCard(card, saved);
  const [picked, setPicked] = useState(!already);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await saveWatch(convId, itemKey);
      setPicked(false); // its 已在观察区 badge now marks it
      onSaved();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="cand-cards">
      <div className="cand-card">
        <input type="checkbox" aria-label={`选择 ${card.ticker}`} checked={picked} onChange={() => setPicked(!picked)} />
        <div className="cand-body">
          <div className="cand-head">
            <span className="cand-label">{card.ticker}</span>
            {already && <span className="cand-badge">已在观察区</span>}
          </div>
          {card.waits.map((w) => (
            <div key={`${w.date} ${w.what}`}><span className="watch-date">{w.date.slice(5)}</span> {w.what}</div>
          ))}
          <div className="earn-sub">到时核对：{card.recheck}</div>
          {saved !== null && !already && <div className="watch-replaces">{replacesText(saved)}</div>}
        </div>
      </div>
      {error && <div className="banner-error">{error}</div>}
      <button className={`text-btn cand-save ${picked ? 'cand-save-on' : ''}`} disabled={busy || !picked} onClick={save}>
        加入观察区
      </button>
    </div>
  );
}
