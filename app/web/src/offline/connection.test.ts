import { beforeEach, describe, expect, it } from 'vitest';
import { getConnection, reportAnswer, reportUnreachable, resetConnection } from './connection';

const offline = (path: string, savedAt: string) => {
  const res = new Response('{}', { headers: { 'X-Lynch-Offline': '1', 'X-Lynch-Saved-At': savedAt } });
  Object.defineProperty(res, 'url', { value: `http://127.0.0.1:31510${path}` });
  return res;
};

beforeEach(resetConnection);

describe('reportAnswer', () => {
  it('an offline-stamped home list goes offline with its saved time', () => {
    reportAnswer(offline('/api/tickers', '2026-10-18T12:00:00Z'));
    expect(getConnection()).toEqual({ offline: true, savedAt: '2026-10-18T12:00:00Z' });
  });

  it('a plain 200 goes online', () => {
    reportAnswer(offline('/api/tickers', '2026-10-18T12:00:00Z'));
    reportAnswer(new Response('{}'));
    expect(getConnection()).toEqual({ offline: false });
  });

  it('a 401 is an answer: online', () => {
    reportUnreachable();
    reportAnswer(new Response('{}', { status: 401 }));
    expect(getConnection()).toEqual({ offline: false });
  });

  it('an offline doc keeps the home list saved time', () => {
    reportAnswer(offline('/api/tickers', '2026-10-18T12:00:00Z'));
    reportAnswer(offline('/api/frameworks/AAA.US', '2026-10-18T13:00:00Z'));
    expect(getConnection()).toEqual({ offline: true, savedAt: '2026-10-18T12:00:00Z' });
  });

  it('nothing answering at all is offline', () => {
    reportUnreachable();
    expect(getConnection()).toEqual({ offline: true, savedAt: null });
  });
});
