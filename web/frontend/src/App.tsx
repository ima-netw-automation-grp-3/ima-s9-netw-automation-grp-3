import { useEffect, type ReactNode } from 'react';
import {
  ActionIcon,
  AppShell,
  Box,
  Burger,
  Group,
  Loader,
  ScrollArea,
  Text,
  Tooltip,
  useComputedColorScheme,
  useMantineColorScheme,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { IconApi, IconChartLine, IconLayoutDashboard, IconMoon, IconSun } from '@tabler/icons-react';
import { Link, Route, Routes, useLocation } from 'react-router';

import { Led, StaleDataAlert, StatusDot, toneOf } from './components/common';
import { useLab, useMonitoring } from './hooks';
import { Dashboard } from './pages/Dashboard';
import { NodePage } from './pages/NodePage';
import { TerminalPage } from './pages/TerminalPage';
import type { LabState } from './types';
import { grafanaUrl, runnerDisplayState, runnerState, stateColor } from './utils';

function NavRow({ to, active, icon, label, meta }: { to: string; active: boolean; icon: ReactNode; label: string; meta?: string | null }) {
  return (
    <Link to={to} className="nav-row" data-active={active || undefined} aria-current={active ? 'page' : undefined}>
      <span className="nav-icon">{icon}</span>
      <span>{label}</span>
      {meta && <span className="nav-meta">{meta}</span>}
    </Link>
  );
}

function Navigation({ lab }: { lab?: LabState }) {
  const { pathname } = useLocation();
  const section = (title: string, role: 'router' | 'client') => {
    const nodes = lab?.nodes.filter((n) => n.role === role) ?? [];
    if (!nodes.length) return null;
    return (
      <>
        <div className="nav-label">{title}</div>
        {nodes.map((n) => (
          <NavRow
            key={n.name}
            to={`/nodes/${n.name}`}
            active={pathname === `/nodes/${n.name}`}
            icon={<StatusDot phase={n.phase} />}
            label={n.name}
            meta={n.mgmt_ipv4}
          />
        ))}
      </>
    );
  };
  return (
    <>
      <AppShell.Section grow component={ScrollArea}>
        <NavRow to="/" active={pathname === '/'} icon={<IconLayoutDashboard size={16} />} label="Tableau de bord" />
        {section('Routeurs SONiC', 'router')}
        {section('Clients Alpine', 'client')}
      </AppShell.Section>
      {lab && (
        <AppShell.Section px={12} py={8}>
          <Text fz="xs" className="muted">
            Lab <span className="mono">{lab.name}</span>
          </Text>
          <Text fz="xs" className="mono muted">
            mgmt {lab.mgmt.subnet ?? '—'}
          </Text>
        </AppShell.Section>
      )}
    </>
  );
}

/** État du déploiement dans l'en-tête : discret quand le lab est déployé, coloré sinon. */
function RunnerStatus({ lab }: { lab: LabState }) {
  const meta = runnerState(runnerDisplayState(lab.runner));
  return (
    <Group gap={8} wrap="nowrap" visibleFrom="xs">
      {meta.busy ? <Loader size={12} color={stateColor(meta.color)} /> : <Led tone={toneOf(meta.color)} />}
      <Text size="sm" c={meta.color === 'green' ? 'dimmed' : stateColor(meta.color)}>
        {meta.label}
      </Text>
    </Group>
  );
}

/** L'API ne répond plus mais des données sont encore affichées : on le dit, sans les masquer. */
function StaleLabAlert() {
  const { data, error, dataUpdatedAt } = useLab();
  if (!error || !data) return null;
  return <StaleDataAlert error={error} updatedAt={dataUpdatedAt} mb="lg" />;
}

function Shell() {
  const [opened, { toggle, close }] = useDisclosure();
  const { data: lab } = useLab();
  const { data: monitoring } = useMonitoring();
  const metricsUrl = grafanaUrl(monitoring, 'overview');
  const { setColorScheme } = useMantineColorScheme();
  const scheme = useComputedColorScheme('light');
  const location = useLocation();
  useEffect(() => close(), [location.pathname, close]);
  useEffect(() => {
    if (lab?.name) document.title = lab.name;
  }, [lab?.name]);
  const themeLabel = scheme === 'dark' ? 'Thème clair' : 'Thème sombre';

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 248, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding={{ base: 'md', sm: 'lg' }}
    >
      <AppShell.Header>
        <Group h="100%" px={20} justify="space-between" wrap="nowrap">
          <Group gap="md" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Navigation" />
            <Link to="/" className="brand">
              {lab?.name ?? ''}
            </Link>
            {lab && <RunnerStatus lab={lab} />}
          </Group>
          <Group gap={4} wrap="nowrap">
            <Tooltip label="Métrologie : tableaux de bord Grafana">
              <ActionIcon
                component="a"
                href={metricsUrl}
                target="_blank"
                rel="noreferrer"
                size={36}
                aria-label="Métrologie (Grafana)"
                disabled={!metricsUrl}
              >
                <IconChartLine size={18} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Documentation de l'API (OpenAPI)">
              <ActionIcon component="a" href="/api/docs" target="_blank" size={36} aria-label="Documentation de l'API (OpenAPI)">
                <IconApi size={18} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label={themeLabel}>
              <ActionIcon size={36} aria-label={themeLabel} onClick={() => setColorScheme(scheme === 'dark' ? 'light' : 'dark')}>
                {scheme === 'dark' ? <IconSun size={18} /> : <IconMoon size={18} />}
              </ActionIcon>
            </Tooltip>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p={8}>
        <Navigation lab={lab} />
      </AppShell.Navbar>
      <AppShell.Main>
        <Box maw={1600}>
          <StaleLabAlert />
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/nodes/:name" element={<NodePage />} />
            <Route path="*" element={<Text c="dimmed">Page introuvable</Text>} />
          </Routes>
        </Box>
      </AppShell.Main>
    </AppShell>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/terminal/:name" element={<TerminalPage />} />
      <Route path="*" element={<Shell />} />
    </Routes>
  );
}
