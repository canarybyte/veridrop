import { Link, NavLink } from "react-router-dom";

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? "app-nav-link is-active" : "app-nav-link";

export function Header() {
  return (
    <header className="app-header">
      <div className="app-header-inner">
        <Link className="app-brand" to="/">
          <span className="app-brand-mark" aria-hidden="true" />
          <span className="app-brand-name">Veridrop</span>
          <span className="app-brand-sub">控制台</span>
        </Link>
        <nav className="app-nav" aria-label="主导航">
          <NavLink to="/" end className={navLinkClass}>
            控制台
          </NavLink>
          <NavLink to="/reports" className={navLinkClass}>
            历史报告
          </NavLink>
          <a className="app-nav-link app-nav-legacy" href="/">
            旧站首页
          </a>
        </nav>
      </div>
    </header>
  );
}
