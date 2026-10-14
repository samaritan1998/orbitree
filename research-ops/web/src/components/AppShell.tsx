import { useState, type ReactElement, type ReactNode } from "react";
import {
  Add20Regular,
  Beaker20Regular,
  BranchFork20Regular,
  BrainCircuit20Regular,
  BookToolbox20Regular,
  ChevronDown20Regular,
  ChevronRight20Regular,
  FolderOpen20Regular,
  Home20Regular,
  MoreHorizontal20Regular,
  Navigation20Regular,
  PaintBrush20Regular,
  PanelLeftContract20Regular,
  PanelLeftExpand20Regular,
  ServerMultiple20Regular,
  Settings20Regular,
  WindowMultiple20Regular,
  WindowConsole20Regular,
} from "@fluentui/react-icons";
import {
  Button,
  Menu,
  MenuItem,
  MenuItemRadio,
  MenuList,
  MenuPopover,
  MenuTrigger,
  Tooltip,
} from "@fluentui/react-components";
import type { Program } from "../types";
import { pagePath, type Page } from "../router";
import { DISPLAY_TIME_ZONE_LABEL } from "../time";

type Props = {
  page: Page;
  programs: Program[];
  onNavigate: (page: Page) => void;
  onNewWork: () => void;
  children: ReactNode;
};

const MORE_ITEMS: Array<{ page: Page; label: string; icon: ReactElement }> = [
  { page: { type: "skills" }, label: "技能", icon: <BookToolbox20Regular /> },
  { page: { type: "evals" }, label: "评测", icon: <Beaker20Regular /> },
  { page: { type: "merge" }, label: "合并队列", icon: <BranchFork20Regular /> },
  { page: { type: "repositories" }, label: "仓库", icon: <FolderOpen20Regular /> },
  { page: { type: "memory", view: "ledger" }, label: "记忆账本", icon: <BrainCircuit20Regular /> },
  { page: { type: "skills", view: "proposals" }, label: "技能审核", icon: <BookToolbox20Regular /> },
];

function MoreMenu({ page, onNavigate, mobile = false }: Pick<Props, "page" | "onNavigate"> & { mobile?: boolean }) {
  const items = mobile
    ? [{ page: { type: "terminal" } as Page, label: "终端", icon: <WindowConsole20Regular /> }, ...MORE_ITEMS, { page: { type: "settings" } as Page, label: "设置", icon: <Settings20Regular /> }]
    : MORE_ITEMS;
  const active = items.some((item) => pagePath(item.page) === pagePath(page));
  return <Menu positioning={mobile ? "above-end" : "after-bottom"}>
    <MenuTrigger disableButtonEnhancement>
      <button className={`nav-item${active ? " active" : ""}`} aria-label="更多" title="更多" aria-current={active ? "page" : undefined}>
        <MoreHorizontal20Regular /><span>更多</span>
      </button>
    </MenuTrigger>
    <MenuPopover className="navigation-popover">
      <MenuList aria-label="更多功能">
        {items.map((item) => <MenuItem
          key={pagePath(item.page)}
          icon={item.icon}
          aria-current={pagePath(item.page) === pagePath(page) ? "page" : undefined}
          onClick={() => onNavigate(item.page)}
        >{item.label}</MenuItem>)}
      </MenuList>
    </MenuPopover>
  </Menu>;
}

function ProgramMenu({ page, programs, onNavigate, mobile = false }: Pick<Props, "page" | "programs" | "onNavigate"> & { mobile?: boolean }) {
  return <Menu positioning={mobile ? "above" : "after-top"}>
    <MenuTrigger disableButtonEnhancement>
      <button className={`nav-item${page.type === "program" ? " active" : ""}`} aria-label="工作方向" title="工作方向" aria-current={page.type === "program" ? "page" : undefined}>
        <Navigation20Regular /><span>{mobile ? "方向" : "工作方向"}</span>
      </button>
    </MenuTrigger>
    <MenuPopover className="navigation-popover">
      <MenuList aria-label="工作方向">
        {programs.map((program) => <MenuItem
          key={program.id}
          icon={<span className="program-swatch" style={{ backgroundColor: program.color }} />}
          aria-current={page.type === "program" && page.code === program.code ? "page" : undefined}
          onClick={() => onNavigate({ type: "program", code: program.code })}
        ><span className="program-menu-label">{program.name}<small>{program.code}</small></span></MenuItem>)}
      </MenuList>
    </MenuPopover>
  </Menu>;
}

const AMBIENCE_STORAGE_KEY = "research-ops:ambience";
const AMBIENCE_OPTIONS = [
  { id: "person-of-interest", label: "Person of Interest", detail: "Surveillance cityscape" },
  { id: "interstellar", label: "Interstellar", detail: "Black-hole horizon" },
  { id: "golden-kamuy", label: "Golden Kamuy", detail: "Hokkaido winter" },
  { id: "observatory", label: "Observatory", detail: "Quiet night shift" },
  { id: "paper", label: "Paper", detail: "No background image" },
] as const;

type AmbienceId = (typeof AMBIENCE_OPTIONS)[number]["id"];

function isAmbienceId(value: string | null): value is AmbienceId {
  return AMBIENCE_OPTIONS.some((option) => option.id === value);
}

function AmbienceMenu({ value, onChange, compact = false }: {
  value: AmbienceId;
  onChange: (value: AmbienceId) => void;
  compact?: boolean;
}) {
  const current = AMBIENCE_OPTIONS.find((option) => option.id === value) ?? AMBIENCE_OPTIONS[0];

  return (
    <Menu
      checkedValues={{ ambience: [value] }}
      onCheckedValueChange={(_, data) => {
        const next = data.checkedItems[0];
        if (isAmbienceId(next)) onChange(next);
      }}
    >
      <MenuTrigger disableButtonEnhancement>
        <Button
          className="ambience-trigger"
          appearance="subtle"
          icon={<PaintBrush20Regular />}
          aria-label={`Choose background theme, current ${current.label}`}
          title={`Background: ${current.label}`}
        >
          {!compact && <span className="ambience-trigger-label">{current.label}</span>}
        </Button>
      </MenuTrigger>
      <MenuPopover className="ambience-popover">
        <MenuList aria-label="Background theme">
          {AMBIENCE_OPTIONS.map((option) => (
            <MenuItemRadio
              key={option.id}
              name="ambience"
              value={option.id}
              aria-label={option.label}
              icon={<span className={`ambience-swatch ambience-swatch-${option.id}`} aria-hidden="true" />}
            >
              <span className="ambience-option-copy">
                <strong>{option.label}</strong>
                <small>{option.detail}</small>
              </span>
            </MenuItemRadio>
          ))}
        </MenuList>
      </MenuPopover>
    </Menu>
  );
}

export default function AppShell({ page, programs, onNavigate, onNewWork, children }: Props) {
  const [programsExpanded, setProgramsExpanded] = useState(() => {
    try {
      return window.localStorage.getItem("research-ops:programs") !== "collapsed";
    } catch {
      return true;
    }
  });
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("research-ops:sidebar") === "collapsed";
    } catch {
      return false;
    }
  });
  const [ambience, setAmbience] = useState<AmbienceId>(() => {
    try {
      const stored = window.localStorage.getItem(AMBIENCE_STORAGE_KEY);
      return isAmbienceId(stored) ? stored : "observatory";
    } catch {
      return "observatory";
    }
  });

  const toggleSidebar = () => {
    setSidebarCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem("research-ops:sidebar", next ? "collapsed" : "expanded");
      } catch {
        // A blocked storage backend should not prevent the local toggle.
      }
      return next;
    });
  };

  const changeAmbience = (next: AmbienceId) => {
    setAmbience(next);
    try {
      window.localStorage.setItem(AMBIENCE_STORAGE_KEY, next);
    } catch {
      // The visual choice remains usable when storage is unavailable.
    }
  };

  const togglePrograms = () => {
    setProgramsExpanded((current) => {
      const next = !current;
      try {
        window.localStorage.setItem("research-ops:programs", next ? "expanded" : "collapsed");
      } catch {
        // Keep navigation usable when storage is unavailable.
      }
      return next;
    });
  };
  const resourcesActive = page.type === "clusters" || page.type === "ltp-monitor";
  const reflectionActive = page.type === "memory" && page.view !== "ledger";

  return (
    <div className={sidebarCollapsed ? "app-shell sidebar-collapsed" : "app-shell"} data-ambience={ambience}>
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><Navigation20Regular /></div>
          <div className="brand-copy">
            <strong>Orbitree</strong>
            <span>Execution workspace</span>
            <small className="timezone-label">{DISPLAY_TIME_ZONE_LABEL}</small>
          </div>
          <Tooltip content={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"} relationship="label">
            <Button
              className="sidebar-toggle"
              appearance="subtle"
              icon={sidebarCollapsed ? <PanelLeftExpand20Regular /> : <PanelLeftContract20Regular />}
              aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              aria-pressed={sidebarCollapsed}
              title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              onClick={toggleSidebar}
            />
          </Tooltip>
        </div>

        <nav className="primary-nav" aria-label="Primary navigation">
          <button
            className={page.type === "today" ? "nav-item active" : "nav-item"}
            aria-label="今日"
            title="今日"
            aria-current={page.type === "today" ? "page" : undefined}
            onClick={() => onNavigate({ type: "today" })}
          >
            <Home20Regular />
            <span>今日</span>
          </button>
          <button
            className={page.type === "sessions" ? "nav-item active" : "nav-item"}
            aria-label="会话"
            title="会话"
            aria-current={page.type === "sessions" ? "page" : undefined}
            onClick={() => onNavigate({ type: "sessions" })}
          >
            <WindowMultiple20Regular />
            <span>会话</span>
          </button>
          {sidebarCollapsed ? <ProgramMenu page={page} programs={programs} onNavigate={onNavigate} /> : <>
            <button
              className={`nav-item program-group-toggle${page.type === "program" && !programsExpanded ? " active" : ""}`}
              aria-label="工作方向"
              aria-expanded={programsExpanded}
              aria-controls="sidebar-programs"
              onClick={togglePrograms}
            >
              <Navigation20Regular /><span>工作方向</span>
              {programsExpanded ? <ChevronDown20Regular /> : <ChevronRight20Regular />}
            </button>
            <div id="sidebar-programs" className="program-nav-group" hidden={!programsExpanded}>
              {programs.map((program) => (
                <button
                  key={program.id}
                  className={page.type === "program" && page.code === program.code ? "nav-item active" : "nav-item"}
                  aria-label={program.name}
                  title={program.name}
                  aria-current={page.type === "program" && page.code === program.code ? "page" : undefined}
                  onClick={() => onNavigate({ type: "program", code: program.code })}
                >
                  <span className="program-swatch" style={{ backgroundColor: program.color }} />
                  <span>{program.name}</span>
                  <small>{program.code}</small>
                </button>
              ))}
            </div>
          </>}
          <button
            className={resourcesActive ? "nav-item active" : "nav-item"}
            aria-label="任务与资源"
            title="任务与资源"
            aria-current={resourcesActive ? "page" : undefined}
            onClick={() => onNavigate({ type: resourcesActive ? page.type : "ltp-monitor" })}
          >
            <ServerMultiple20Regular />
            <span>任务与资源</span>
          </button>
          <button
            className={reflectionActive ? "nav-item active" : "nav-item"}
            aria-label="每日复盘"
            title="每日复盘"
            aria-current={reflectionActive ? "page" : undefined}
            onClick={() => onNavigate({ type: "memory" })}
          >
            <BrainCircuit20Regular />
            <span>每日复盘</span>
          </button>
          <button
            className={page.type === "terminal" ? "nav-item active" : "nav-item"}
            aria-label="终端"
            title="终端"
            aria-current={page.type === "terminal" ? "page" : undefined}
            onClick={() => onNavigate({ type: "terminal" })}
          ><WindowConsole20Regular /><span>终端</span></button>
        </nav>

        <div className="sidebar-footer">
          <div className="sidebar-utilities">
            <MoreMenu page={page} onNavigate={onNavigate} />
            <Tooltip content="设置" relationship="label">
              <button
                className={`nav-item settings-trigger${page.type === "settings" ? " active" : ""}`}
                aria-label="设置"
                title="设置"
                aria-current={page.type === "settings" ? "page" : undefined}
                onClick={() => onNavigate({ type: "settings" })}
              ><Settings20Regular /></button>
            </Tooltip>
          </div>
          <AmbienceMenu value={ambience} onChange={changeAmbience} />
          <Button aria-label="New work" title="New work" appearance="primary" icon={<Add20Regular />} onClick={onNewWork}>
            <span className="sidebar-footer-label">New work</span>
          </Button>
        </div>
      </aside>

      <header className="mobile-bar">
        <strong>Orbitree</strong>
        <div className="mobile-bar-actions">
          <AmbienceMenu value={ambience} onChange={changeAmbience} compact />
          <Tooltip content="New work" relationship="label">
            <Button aria-label="New work" appearance="subtle" icon={<Add20Regular />} onClick={onNewWork} />
          </Tooltip>
        </div>
      </header>

      <main className="workspace-main">{children}</main>

      <nav className="mobile-nav" aria-label="Mobile navigation">
        <button aria-label="今日" aria-current={page.type === "today" ? "page" : undefined} onClick={() => onNavigate({ type: "today" })}><Home20Regular /><span>今日</span></button>
        <button aria-label="会话" aria-current={page.type === "sessions" ? "page" : undefined} onClick={() => onNavigate({ type: "sessions" })}><WindowMultiple20Regular /><span>会话</span></button>
        <ProgramMenu page={page} programs={programs} onNavigate={onNavigate} mobile />
        <button aria-label="任务与资源" aria-current={resourcesActive ? "page" : undefined} onClick={() => onNavigate({ type: resourcesActive ? page.type : "ltp-monitor" })}><ServerMultiple20Regular /><span>任务</span></button>
        <button aria-label="每日复盘" aria-current={reflectionActive ? "page" : undefined} onClick={() => onNavigate({ type: "memory" })}><BrainCircuit20Regular /><span>复盘</span></button>
        <MoreMenu page={page} onNavigate={onNavigate} mobile />
      </nav>
    </div>
  );
}
