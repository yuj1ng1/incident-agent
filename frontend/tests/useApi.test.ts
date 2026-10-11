import { act, renderHook, waitFor } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useApi } from '../src/hooks/useApi';
import { json } from './fixtures';

it('aborts replaced requests and ignores late results after route changes', async () => {
  let finishFirst!: (response: Response) => void;
  const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { finishFirst = resolve; }))
    .mockResolvedValueOnce(json({ value: 'new result' }));
  vi.stubGlobal('fetch', fetch);
  const { result, rerender } = renderHook(path => useApi<{ value: string }>(path), { initialProps: '/api/sessions/first' });
  const firstSignal = fetch.mock.calls[0][1].signal as AbortSignal;
  rerender('/api/sessions/second');
  await waitFor(() => expect(result.current.data?.value).toBe('new result'));
  expect(firstSignal.aborted).toBe(true);
  await act(async () => {
    finishFirst(json({ value: 'late stale result' }));
    await new Promise(resolve => setTimeout(resolve, 0));
  });
  expect(result.current.loading).toBe(false);
  expect(result.current.data?.value).toBe('new result');
});
