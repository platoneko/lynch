import { describe, expect, it } from 'vitest';
import { cancelEdit, composer, draftText, sent, startEdit, typed } from './composer';

describe('composer', () => {
  it('typing outside an edit is the draft', () => {
    const c = typed(composer('a'), 'ab');
    expect(c.text).toBe('ab');
    expect(draftText(c)).toBe('ab');
  });

  it('an edit fills the input but keeps the unsent draft aside', () => {
    const c = startEdit(composer('unsent'), 'u1', 'old message');
    expect(c).toMatchObject({ text: 'old message', editing: 'u1' });
    expect(draftText(c)).toBe('unsent');
    expect(draftText(typed(c, 'old message, reworded'))).toBe('unsent');
  });

  it('cancelling an edit brings the draft back', () => {
    const c = cancelEdit(typed(startEdit(composer('unsent'), 'u1', 'old'), 'changed'));
    expect(c).toEqual(composer('unsent'));
  });

  it('switching to another message keeps the original draft aside', () => {
    const c = startEdit(startEdit(composer('unsent'), 'u1', 'first'), 'u2', 'second');
    expect(c).toMatchObject({ text: 'second', editing: 'u2' });
    expect(cancelEdit(c)).toEqual(composer('unsent'));
  });

  it('a sent edit brings the draft back: it was never sent', () => {
    const c = typed(startEdit(composer('unsent'), 'u1', 'old'), 'new');
    expect(sent(c, 'new')).toEqual(composer('unsent'));
  });

  it('a sent message clears the input', () => {
    expect(sent(composer('hello'), 'hello')).toEqual(composer(''));
    expect(sent(composer('  hello \n'), 'hello')).toEqual(composer(''));
  });

  it('text typed while the send was in flight survives it', () => {
    expect(sent(composer('next thought'), 'hello')).toEqual(composer('next thought'));
  });
});
