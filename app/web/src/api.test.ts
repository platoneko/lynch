import { afterEach, describe, expect, it, vi } from 'vitest';
import { get, post } from './api';

function answer(status: number, body: string): void {
  vi.stubGlobal('fetch', async () => new Response(body, { status }));
}

describe('post', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('always sends a JSON body, {} when there is none (the server refuses anything else)', async () => {
    const seen: RequestInit[] = [];
    vi.stubGlobal('fetch', async (_: string, init: RequestInit) => {
      seen.push(init);
      return new Response(null, { status: 204 });
    });
    await post('/x');
    await post('/y', { text: 'hi' });
    expect(seen.map((i) => [i.method, (i.headers as Record<string, string>)['Content-Type'], i.body])).toEqual([
      ['POST', 'application/json', '{}'],
      ['POST', 'application/json', '{"text":"hi"}'],
    ]);
  });
  it('surfaces the server error verbatim', async () => {
    answer(409, JSON.stringify({ error: '对话正在运行' }));
    await expect(post('/x', {})).rejects.toThrow('对话正在运行');
  });
  it('falls back to the body text when it is not JSON', async () => {
    answer(502, 'proxy: server down');
    await expect(post('/x')).rejects.toThrow('proxy: server down');
  });
  it('falls back to the status when the body is empty', async () => {
    answer(409, '');
    await expect(post('/x')).rejects.toThrow('409');
    answer(400, JSON.stringify({ error: '' }));
    await expect(post('/x')).rejects.toThrow('400');
  });
});

describe('unpaired device', () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubLocation(): ReturnType<typeof vi.fn> {
    const assign = vi.fn();
    vi.stubGlobal('window', { location: { assign } });
    return assign;
  }

  it('get sends the page to /pair on 401 and still throws', async () => {
    const assign = stubLocation();
    answer(401, JSON.stringify({ error: '未配对或凭证已吊销' }));
    await expect(get('/api/tickers')).rejects.toThrow('401');
    expect(assign).toHaveBeenCalledWith('/pair');
  });

  it('post sends the page to /pair on 401 and still throws', async () => {
    const assign = stubLocation();
    answer(401, JSON.stringify({ error: '未配对或凭证已吊销' }));
    await expect(post('/api/chats', {})).rejects.toThrow('未配对或凭证已吊销');
    expect(assign).toHaveBeenCalledWith('/pair');
  });

  it('other failures stay on the page', async () => {
    const assign = stubLocation();
    answer(403, 'forbidden');
    await expect(get('/x')).rejects.toThrow('403');
    answer(409, JSON.stringify({ error: '对话正在运行' }));
    await expect(post('/x')).rejects.toThrow('对话正在运行');
    expect(assign).not.toHaveBeenCalled();
  });
});
