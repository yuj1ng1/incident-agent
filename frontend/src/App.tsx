import { Activity, ArrowUpRight, ChevronRight, Files, LayoutDashboard, LockKeyhole, ScanLine, ShieldCheck } from 'lucide-react';
import { Link, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { apiBaseUrl } from './api/client';
import { EmptyState, PageHeader } from './components/ui';
import { OverviewPage } from './pages/OverviewPage';
import { IncidentListPage, SessionListPage } from './pages/ListPages';
import { IncidentDetailPage } from './pages/IncidentDetailPage';
import { SessionDetailPage } from './pages/SessionDetailPage';
import { RemediationDetailPage, RemediationListPage } from './pages/RemediationPages';
import { OperatorProvider } from './components/OperatorAccess';

export function App() {
  const { pathname } = useLocation();
  const section = pathname.startsWith('/incidents') ? 'Incidents'
    : pathname.startsWith('/sessions') ? 'Sessions' : pathname.startsWith('/remediations') ? 'Remediations' : '总览';
  const detail = /^\/(incidents|sessions|remediations)\/.+/.test(pathname);
  let docsUrl: string | undefined;
  try { docsUrl = `${apiBaseUrl()}/docs`; } catch { /* Request errors are displayed by each page. */ }
  return <OperatorProvider><div className="app-shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <aside className="sidebar">
      <Link className="brand" to="/" aria-label="Incident Agent 总览"><span className="brand-mark"><Activity size={21} aria-hidden="true" /></span><div className="brand-copy">Incident Agent<span>调查工作台</span></div></Link>
      <div className="nav-label">工作空间</div>
      <nav aria-label="主要导航">
        <NavLink to="/" end aria-label="总览" title="总览"><LayoutDashboard size={18} aria-hidden="true" /><span className="nav-text">总览</span></NavLink>
        <NavLink to="/incidents" aria-label="Incidents" title="Incidents"><Files size={18} aria-hidden="true" /><span className="nav-text">Incidents</span></NavLink>
        <NavLink to="/sessions" aria-label="Sessions" title="Sessions"><ScanLine size={18} aria-hidden="true" /><span className="nav-text">Sessions</span></NavLink>
        <NavLink to="/remediations" aria-label="Remediations" title="Remediations"><ShieldCheck size={18} aria-hidden="true" /><span className="nav-text">Remediations</span></NavLink>
      </nav>
      <div className="sidebar-footer"><div className="local-status" title="本地 · 受控授权"><LockKeyhole size={14} aria-hidden="true" /><span className="sidebar-copy">本地 · 受控授权</span></div>
        {docsUrl && <a href={docsUrl} target="_blank" rel="noreferrer" aria-label="API 文档" title="API 文档"><span className="sidebar-copy">API 文档</span><ArrowUpRight size={14} aria-hidden="true" /></a>}
        <span className="version sidebar-copy">v0.1</span>
      </div>
    </aside>
    <div className="workspace"><div className="topbar"><nav className="breadcrumbs" aria-label="面包屑"><span className="workspace-name">工作台</span><ChevronRight size={14} aria-hidden="true" />
      {detail ? <><Link to={section === 'Incidents' ? '/incidents' : section === 'Sessions' ? '/sessions' : '/remediations'}>{section}</Link><ChevronRight size={14} aria-hidden="true" /><span aria-current="page">详情</span></> : <span aria-current="page">{section}</span>}
      </nav><span className="read-only"><LockKeyhole size={13} aria-hidden="true" />{section === 'Remediations' ? '写操作需后端授权' : '只读模式'}</span></div>
      <main id="main-content"><Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/incidents" element={<IncidentListPage />} />
        <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
        <Route path="/sessions" element={<SessionListPage />} />
        <Route path="/sessions/:sessionId" element={<SessionDetailPage />} />
        <Route path="/remediations" element={<RemediationListPage />} />
        <Route path="/remediations/:proposalId" element={<RemediationDetailPage />} />
        <Route path="*" element={<><PageHeader title="页面不存在" description="请使用左侧导航返回工作台。" /><EmptyState title="没有找到这个页面" /></>} />
      </Routes></main>
      <footer className="workspace-footer"><span>Incident Agent</span><span>北京时间 · 按需读取</span></footer>
    </div>
  </div></OperatorProvider>;
}
