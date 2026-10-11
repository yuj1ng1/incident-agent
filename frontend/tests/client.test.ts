import { describe, expect, it, vi } from 'vitest';
import { apiBaseUrl, getJson } from '../src/api/client';
import { pageQuery, formatTime, recordPath } from '../src/lib/format';
import { json, page } from './fixtures';

describe('local read-only API requests', () => {
  it.each(['https://127.0.0.1:8010', 'http://remote.example', 'http://127.0.0.1:8010/path',
    'http://user:secret@localhost:8010', 'http://evil.localhost:8010', 'http://localhost:8010/?token=value', 'invalid'])('rejects unsafe base URL %s', value => {
    expect(() => apiBaseUrl(value)).toThrow();
  });
  it('accepts a custom loopback port and sends GET without credentials or caching', async () => {
    expect(apiBaseUrl('http://localhost:8011')).toBe('http://localhost:8011');
    const fetch = vi.fn().mockResolvedValue(json(page([])));
    vi.stubGlobal('fetch', fetch);
    await expect(getJson('/api/incidents?limit=20', new AbortController().signal)).resolves.toEqual(page([]));
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:8010/api/incidents?limit=20', expect.objectContaining({ method: 'GET', credentials: 'omit', cache: 'no-store' }));
  });
  it.each([404, 422, 500, 503])('reports HTTP %s without exposing raw error content', async status => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ detail: 'raw-private-error' }, status)));
    try { await getJson('/api/sessions/absent', new AbortController().signal); throw new Error('must fail'); }
    catch (error) { expect((error as Error).message).not.toContain('raw-private-error'); expect((error as Error).message).not.toBe('must fail'); }
  });
  it.each([
    new Response('<html>wrong port</html>', { headers: { 'Content-Type': 'text/html' } }),
    new Response('{', { headers: { 'Content-Type': 'application/json' } }),
    json(null), json({ items: [], has_more: 'yes' }),
  ])('rejects malformed responses', async response => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
    await expect(getJson('/api/incidents', new AbortController().signal)).rejects.toThrow();
  });
  it('times out a stalled request instead of retrying indefinitely', async () => {
    vi.useFakeTimers();
    try {
      const fetch = vi.fn((_url, options: RequestInit) => new Promise((_resolve, reject) => {
        options.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      }));
      vi.stubGlobal('fetch', fetch);
      const pending = expect(getJson('/api/incidents', new AbortController().signal)).rejects.toThrow('10 秒');
      await vi.advanceTimersByTimeAsync(10000);
      await pending;
      expect(fetch).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
});

describe('query and display boundaries', () => {
  it('normalizes invalid filters and caps offsets before sending requests', () => {
    expect(pageQuery(new URLSearchParams('offset=10001&status=open'), ['investigating', 'resolved'])).toEqual({ offset: 0, status: '' });
    expect(pageQuery(new URLSearchParams('offset=20&status=resolved'), ['investigating', 'resolved'])).toEqual({ offset: 20, status: 'resolved' });
    expect(pageQuery(new URLSearchParams('offset=1.5'), [])).toEqual({ offset: 0, status: '' });
    expect(recordPath('sessions', 'ses/a ?#')).toBe('/sessions/ses%2Fa%20%3F%23');
  });
  it('uses Beijing time for naive and UTC records and preserves missing time', () => {
    expect(formatTime('2026-10-11T09:00:00')).toBe(formatTime('2026-10-11T01:00:00Z'));
    expect(formatTime(null)).toBe('暂无记录');
    expect(formatTime('bad')).toBe('时间无效');
  });
});
