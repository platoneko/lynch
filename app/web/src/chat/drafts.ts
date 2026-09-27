/**
 * Unsent input per conversation, and whether tool calls are shown, kept on the phone; losing either is harmless,
 * so every failure is swallowed.
 */
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

const SHOW_TOOLS = 'lynch.chat.showTools';

/** Tool calls are hidden unless the reader turned them on: on the phone only the agent's replies matter. */
export function loadShowTools(store: DraftStore | null = browserStore()): boolean {
  try {
    return store?.getItem(SHOW_TOOLS) === '1';
  } catch {
    return false;
  }
}

export function saveShowTools(show: boolean, store: DraftStore | null = browserStore()): void {
  try {
    if (show) store?.setItem(SHOW_TOOLS, '1');
    else store?.removeItem(SHOW_TOOLS);
  } catch {
    /* quota or privacy mode: the setting is a convenience */
  }
}
