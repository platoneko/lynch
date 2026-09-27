import { describe, expect, it } from 'vitest';
import { clearDraft, loadDraft, loadShowTools, saveDraft, saveShowTools, type DraftStore } from './drafts';

function memory(): DraftStore & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return { map, getItem: (k) => map.get(k) ?? null, setItem: (k, v) => void map.set(k, v), removeItem: (k) => void map.delete(k) };
}

describe('drafts', () => {
  it('saves, loads and clears per key', () => {
    const s = memory();
    saveDraft('c1', 'half typed', s);
    expect(loadDraft('c1', s)).toBe('half typed');
    expect(loadDraft('c2', s)).toBe('');
    clearDraft('c1', s);
    expect(s.map.size).toBe(0);
  });
  it('an empty draft removes the entry', () => {
    const s = memory();
    saveDraft('c1', 'x', s);
    saveDraft('c1', '', s);
    expect(s.map.size).toBe(0);
  });
  it('survives a storage that throws or is missing', () => {
    const broken: DraftStore = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('full'); }, removeItem: () => {} };
    expect(loadDraft('c1', broken)).toBe('');
    expect(() => saveDraft('c1', 'x', broken)).not.toThrow();
    expect(loadDraft('c1', null)).toBe('');
  });
  it('remembers whether tool calls are shown, hidden by default', () => {
    const s = memory();
    expect(loadShowTools(s)).toBe(false);
    saveShowTools(true, s);
    expect(loadShowTools(s)).toBe(true);
    saveShowTools(false, s);
    expect(loadShowTools(s)).toBe(false);
    expect(loadShowTools(null)).toBe(false);
  });
});
