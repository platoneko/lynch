import { afterEach, describe, expect, it, vi } from 'vitest';
import { post } from './api';

function answer(status: number, body: string): void {
  vi.stubGlobal('fetch', async () => new Response(body, { status }));
}

describe('post', () => {
  afterEach(() => vi.unstubAllGlobals());

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
