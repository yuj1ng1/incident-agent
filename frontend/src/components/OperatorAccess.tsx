import { createContext, useContext, useRef, useState, type ReactNode } from 'react';
import { operatorJson } from '../api/client';
import type { OperatorView } from '../api/types';

const Context = createContext<{
  token: string; operator: OperatorView | null;
  connect: (value: string) => Promise<void>; disconnect: () => void;
}>({ token: '', operator: null, connect: async () => {}, disconnect: () => {} });

export function OperatorProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<{ token: string; operator: OperatorView | null }>({ token: '', operator: null });
  const generation = useRef(0);
  const disconnect = () => { generation.current++; setSession({ token: '', operator: null }); };
  const connect = async (token: string) => {
    const current = ++generation.current;
    setSession({ token: '', operator: null });
    const operator = await operatorJson<OperatorView>('/api/operator', token, new AbortController().signal);
    if (current === generation.current) setSession({ token, operator });
  };
  return <Context.Provider value={{ ...session, connect, disconnect }}>{children}</Context.Provider>;
}

export const useOperator = () => useContext(Context);

export function OperatorAccess() {
  const { operator, connect, disconnect } = useOperator();
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const locked = useRef(false);
  return <div className="operator-access">
    {operator ? <><p>操作身份：<strong>{operator.actor}</strong> · 权限：{operator.permissions.join(' / ')}</p>
      <button className="button" onClick={disconnect}>断开操作身份</button></> : <form onSubmit={async event => {
        event.preventDefault();
        if (locked.current) return;
        locked.current = true; setBusy(true); setError('');
        try { await connect(input); setInput(''); }
        catch (failure) { setError((failure as Error).message); }
        finally { locked.current = false; setBusy(false); }
      }}>
      <label>本地操作凭据<input type="password" autoComplete="off" value={input} onChange={e => setInput(e.target.value)} required minLength={32} maxLength={512} /></label>
      <button className="button" disabled={busy || input.length < 32}>{busy ? '正在验证…' : '连接操作身份'}</button>
      <p>写操作默认关闭。凭据仅保存在本页内存中；刷新页面后需重新输入。身份和权限由后端确认。</p>
    </form>}
    {error && <p role="alert" className="operation-error">{error}</p>}
  </div>;
}
