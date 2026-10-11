import { Activity, ArrowUpRight, Files, LayoutDashboard, LockKeyhole, ScanLine } from 'lucide-react';
import { NavLink, Route, Routes } from 'react-router-dom';
import { apiBaseUrl } from './api/client';
import { EmptyState, PageHeader } from './components/ui';
import { OverviewPage } from './pages/OverviewPage';
import { IncidentListPage, SessionListPage } from './pages/ListPages';
import { IncidentDetailPage } from './pages/IncidentDetailPage';
import { SessionDetailPage } from './pages/SessionDetailPage';

export function App() {
  let docsUrl: string | undefined;
  try { docsUrl = `${apiBaseUrl()}/docs`; } catch { /* Request errors are displayed by each page. */ }
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Activity size={23} /></span><div>Incident Agent<span>调查工作台</span></div></div>
      <div className="nav-label">工作空间</div>
      <nav aria-label="主要导航">
        <NavLink to="/" end><LayoutDashboard size={18} />总览</NavLink>
        <NavLink to="/incidents"><Files size={18} />Incidents</NavLink>
        <NavLink to="/sessions"><ScanLine size={18} />Sessions</NavLink>
      </nav>
      <div className="sidebar-footer"><div className="local-status"><span />本地 · 只读</div>
        <p>查看真实告警与调查记录。</p>
        {docsUrl && <a href={docsUrl} target="_blank" rel="noreferrer">API 文档<ArrowUpRight size={14} /></a>}
        <span className="version">第一版工作台 / v0.1</span>
      </div>
    </aside>
    <div className="workspace"><div className="topbar"><span>Incident Agent <span className="topbar-separator">/</span> 本地工作台</span><span className="read-only"><LockKeyhole size={13} />只读模式</span></div>
      <main id="main-content"><Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/incidents" element={<IncidentListPage />} />
        <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
        <Route path="/sessions" element={<SessionListPage />} />
        <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
        <Route path="*" element={<><PageHeader title="页面不存在" description="请使用左侧导航返回工作台。" /><EmptyState title="没有找到这个页面" /></>} />
      </Routes></main>
      <footer className="workspace-footer"><span>Incident Agent · 本地只读工作台</span><span>所有时间均为北京时间 · 数据按需读取</span></footer>
    </div>
  </div>;
}
