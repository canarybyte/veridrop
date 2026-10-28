import { Link, Route, Routes } from "react-router-dom";

import { Header } from "./components/header";
import ConsolePage from "./pages/console";
import ReportsPage from "./pages/reports";
import ReportDetailPage from "./pages/report-detail";
import CredentialsPage from "./pages/credentials";

function NotFound() {
  return (
    <div className="empty-state">
      <h1 className="page-title">页面不存在</h1>
      <p className="page-subtitle">
        你访问的地址不在控制台范围内，<Link className="text-link" to="/">返回控制台</Link>。
      </p>
    </div>
  );
}

export default function App() {
  return (
    <div className="app-shell">
      <Header />
      <main className="app-main">
        <Routes>
          <Route path="/" element={<ConsolePage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/reports/:id" element={<ReportDetailPage />} />
          <Route path="/credentials" element={<CredentialsPage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
