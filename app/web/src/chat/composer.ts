/**
 * The chat input. Editing an earlier message borrows the input, so the unsent draft is set aside
 * (`stash`) and comes back when the edit is cancelled or sent: a draft goes away only once it is sent.
 */
export interface Composer {
  text: string; // what the input shows
  editing: string | null; // uuid of the message being rewritten
  stash: string; // the unsent draft while `editing`
}

export function composer(text: string): Composer {
  return { text, editing: null, stash: '' };
}

/** What the conversation's stored draft should hold: never the text of an edit. */
export function draftText(c: Composer): string {
  return c.editing === null ? c.text : c.stash;
}

export function typed(c: Composer, text: string): Composer {
  return { ...c, text };
}

export function startEdit(c: Composer, uuid: string, text: string): Composer {
  return { text, editing: uuid, stash: draftText(c) };
}

export function cancelEdit(c: Composer): Composer {
  return composer(draftText(c));
}

/**
 * After `body` went out: an edit gives the input back to the draft; a message clears the input unless
 * the reader already typed something new while it was in flight.
 */
export function sent(c: Composer, body: string): Composer {
  if (c.editing !== null) return cancelEdit(c);
  return composer(c.text.trim() === body ? '' : c.text);
}

/** An unanswered message back in the input, after whatever the reader typed since. Not during an edit. */
export function putBack(c: Composer, text: string): Composer {
  return typed(c, c.text.trim() ? `${c.text.trimEnd()}\n\n${text}` : text);
}
