import { afterEach, describe, expect, it, vi } from 'vitest';
import { get, getIfChanged, post } from './api';

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

describe('getIfChanged', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('asks with the held tag and gets null back when the server says unchanged', async () => {
    const seen: (string | null)[] = [];
    vi.stubGlobal('fetch', async (_: string, init: RequestInit) => {
      seen.push(new Headers(init.headers).get('If-None-Match'));
      return new Response(null, { status: 304 });
    });
    expect(await getIfChanged('/api/tickers', '"abc"')).toBeNull();
    expect(seen).toEqual(['"abc"']);
  });
  it('asks plainly when nothing is held and returns a changed answer', async () => {
    const seen: (string | null)[] = [];
    vi.stubGlobal('fetch', async (_: string, init: RequestInit) => {
      seen.push(new Headers(init.headers).get('If-None-Match'));
      return new Response('body', { status: 200, headers: { ETag: '"new"' } });
    });
    const res = await getIfChanged('/api/tickers', null);
    expect(res?.headers.get('ETag')).toBe('"new"');
    expect(seen).toEqual([null]);
  });
  it('fails like get', async () => {
    answer(500, 'boom');
    await expect(getIfChanged('/x', '"abc"')).rejects.toThrow('500 boom');
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

describe('nothing answers', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('get and post say tailfwd may not be running when fetch itself fails', async () => {
    vi.stubGlobal('fetch', async () => { throw new TypeError('Failed to fetch'); });
    await expect(get('/api/tickers')).rejects.toThrow('连不上 lynch 服务，请确认 tailfwd 在运行');
    await expect(post('/api/chats', {})).rejects.toThrow('连不上 lynch 服务，请确认 tailfwd 在运行');
  });

  it('other fetch failures pass through unchanged', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError');
    vi.stubGlobal('fetch', async () => { throw abort; });
    await expect(get('/x')).rejects.toBe(abort);
  });
});
