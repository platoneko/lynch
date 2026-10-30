import { describe, expect, it } from 'vitest';
import {
  anomaliesHash, axisHash, candidatesHash, chatHash, chatsHash, docHash, earningsHash, newChatHash, newDiscoverHash, parseRoute,
  watchHash,
} from './route';

describe('route', () => {
  it('parses doc and falls back to home', () => {
    expect(parseRoute('#/doc/NVDA.US')).toEqual({ page: 'doc', ticker: 'NVDA.US' });
    expect(parseRoute('')).toEqual({ page: 'home' });
    expect(parseRoute('#/')).toEqual({ page: 'home' });
    expect(parseRoute('#/doc/a/b')).toEqual({ page: 'home' });
  });
  it('parses axis pages', () => {
    expect(parseRoute('#/axis/fed-path-aluminium')).toEqual({ page: 'axis', name: 'fed-path-aluminium' });
    expect(parseRoute('#/axis/a/b')).toEqual({ page: 'home' });
    expect(parseRoute(axisHash('coal-price'))).toEqual({ page: 'axis', name: 'coal-price' });
  });
  it('round-trips', () => {
    expect(parseRoute(docHash('0700.HK'))).toEqual({ page: 'doc', ticker: '0700.HK' });
  });
  it('parses chat pages', () => {
    expect(parseRoute('#/chats')).toEqual({ page: 'chats' });
    expect(parseRoute('#/chat/new')).toEqual({ page: 'chat-new', ticker: null });
    expect(parseRoute('#/chat/new/NVDA.US')).toEqual({ page: 'chat-new', ticker: 'NVDA.US' });
    expect(parseRoute('#/chat/0b1c')).toEqual({ page: 'chat', id: '0b1c' });
    expect(parseRoute(chatHash('a-b'))).toEqual({ page: 'chat', id: 'a-b' });
    expect(parseRoute(newChatHash('0700.HK'))).toEqual({ page: 'chat-new', ticker: '0700.HK' });
    expect(parseRoute(chatsHash())).toEqual({ page: 'chats' });
  });
  it('parses the discover and candidate pages before chat ids', () => {
    expect(parseRoute('#/chat/new-discover')).toEqual({ page: 'chat-new-discover' });
    expect(parseRoute(newDiscoverHash())).toEqual({ page: 'chat-new-discover' });
    expect(parseRoute('#/candidates')).toEqual({ page: 'candidates' });
    expect(parseRoute(candidatesHash())).toEqual({ page: 'candidates' });
  });
  it('parses the earnings page', () => {
    expect(parseRoute('#/earnings')).toEqual({ page: 'earnings' });
    expect(parseRoute(earningsHash())).toEqual({ page: 'earnings' });
  });
  it('parses the anomaly page', () => {
    expect(parseRoute('#/anomalies')).toEqual({ page: 'anomalies' });
    expect(anomaliesHash()).toBe('#/anomalies');
    expect(parseRoute('#/watch')).toEqual({ page: 'watch' });
    expect(watchHash()).toBe('#/watch');
  });
});
