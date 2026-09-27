/** Unsent input per conversation, kept on the phone; losing it is harmless, so every failure is swallowed. */
export type DraftStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

const PREFIX = 'lynch.chat.draft.';

function browserStore(): DraftStore | null {
  try {
    return window.localStorage;
  } catch {
    return null; // no window (tests) or storage blocked
  }
}

export function loadDraft(key: string, store: DraftStore | null = browserStore()): string {
  try {
    return store?.getItem(PREFIX + key) ?? '';
  } catch {
    return '';
  }
}

export function saveDraft(key: string, text: string, store: DraftStore | null = browserStore()): void {
  try {
    if (text) store?.setItem(PREFIX + key, text);
    else store?.removeItem(PREFIX + key);
  } catch {
    /* quota or privacy mode: the draft is a convenience */
  }
}

export function clearDraft(key: string, store: DraftStore | null = browserStore()): void {
  saveDraft(key, '', store);
}
