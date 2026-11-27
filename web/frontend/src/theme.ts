import {
  ActionIcon,
  AppShell,
  Badge,
  Button,
  Card,
  Drawer,
  Menu,
  Modal,
  Popover,
  Table,
  Tabs,
  Text,
  Tooltip,
  createTheme,
  defaultVariantColorsResolver,
  rem,
  type CSSVariablesResolver,
  type VariantColorsResolver,
} from '@mantine/core';

/**
 * Thème de l'interface, aligné sur Grafana (onglets « Métrologie ») : mêmes gris froids (fond #111217,
 * panneaux #181b1f, texte #ccccdc), même bleu primaire, même police (Inter), rayons serrés.
 * Les couleurs d'état reprennent la palette des mappings Grafana (green, orange, red, yellow, blue).
 */

export const EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';

/** Terminal : une console reste sombre dans les deux thèmes ; rampe de Grafana, curseur au bleu des liens. */
export const TERMINAL_THEME = {
  background: '#0b0c0e',
  foreground: '#ccccdc',
  cursor: '#6e9fff',
  cursorAccent: '#0b0c0e',
  selectionBackground: 'rgba(110, 159, 255, 0.3)',
};

// Rampe sombre de Grafana : 0 texte, 2 texte secondaire, 3 discret, 4 filets, 5 relief, 6 panneaux, 7 page, 8 fond profond.
const dark = [
  '#ccccdc',
  '#b6b6c4',
  '#a0a0ad',
  '#8a8b98',
  '#2f3238',
  '#22252b',
  '#181b1f',
  '#111217',
  '#0b0c0e',
  '#08090a',
] as const;

// Rampe claire (thème clair de Grafana) : 0 survol, 1 relief, 3 filets, 4 bords des contrôles, 6 discret, 7 secondaire, 9 texte.
const gray = [
  '#f4f5f5',
  '#eef0f2',
  '#e4e6e9',
  '#dfe1e4',
  '#cdd0d4',
  '#a2a6ad',
  '#676d75',
  '#555b62',
  '#3a3f45',
  '#24292e',
] as const;

// Bleu primaire de Grafana : 4 liens sur fond sombre, 6 bouton plein en clair, 8 bouton plein en sombre (9 : survol, texte blanc lisible).
const blue = [
  '#edf2fd',
  '#dbe5fb',
  '#b7cbf6',
  '#93b1f1',
  '#6e9fff',
  '#5785e2',
  '#3871dc',
  '#2f62c4',
  '#3d71d9',
  '#3466cc',
] as const;

/** Variante « subtle » neutre (icônes d'action, boutons discrets) : sans fond, survol d'un cran de surface. */
const variantColorResolver: VariantColorsResolver = (input) => {
  const base = defaultVariantColorsResolver(input);
  const neutral = !input.color || input.color === 'gray' || input.color === 'dark';
  if (input.variant === 'subtle' && neutral) {
    return {
      background: 'transparent',
      hover: 'var(--app-hover)',
      color: 'var(--mantine-color-dimmed)',
      hoverColor: 'var(--mantine-color-text)',
      border: `${rem(1)} solid transparent`,
    };
  }
  return base;
};

/** Variante « light » en sombre (alertes) : fond teinté (12 %, 10 % pour le rouge), texte dans la couleur d'état. */
const tint = (name: string, rgb: string, text: string, alpha = 0.12) => ({
  [`--mantine-color-${name}-light`]: `rgba(${rgb}, ${alpha})`,
  [`--mantine-color-${name}-light-hover`]: `rgba(${rgb}, ${alpha + 0.06})`,
  [`--mantine-color-${name}-light-color`]: text,
});

export const cssVariablesResolver: CSSVariablesResolver = () => ({
  variables: {
    '--app-ease': EASE,
    '--app-terminal': TERMINAL_THEME.background,
  },
  light: {
    '--mantine-color-body': '#f4f5f5',
    '--mantine-color-dimmed': '#555b62',
    '--mantine-color-placeholder': '#8b9198',
    '--mantine-color-anchor': '#1f62e0',
    '--mantine-color-default': '#ffffff',
    '--mantine-color-default-hover': '#f4f5f5',
    '--mantine-color-default-color': '#24292e',
    '--mantine-color-default-border': '#cdd0d4',
    '--app-surface': '#ffffff',
    '--app-raised': '#eef0f2',
    '--app-deep': '#f7f8f9',
    '--app-hover': 'rgba(36, 41, 46, 0.06)',
    '--app-border': '#dfe1e4',
    '--app-muted': '#676d75',
    '--app-highlight': 'inset 0 0 0 1px #dfe1e4',
    '--app-cable': '#80868e',
    '--app-led-off': 'rgba(36, 41, 46, 0.16)',
    '--app-code-bg': '#eef0f2',
    '--app-tooltip-bg': '#24292e',
    '--app-tooltip-color': '#ffffff',
    '--app-tooltip-border': '#24292e',
    '--app-focus': '#1f62e0',
    '--state-ok': '#197947',
    '--state-warn': '#ab4f09',
    '--state-err': '#c92746',
    '--state-busy': '#8d6700',
    '--state-info': '#3468ca',
    '--state-off': '#7c828a',
  },
  dark: {
    // alertes : teinte légère de la couleur d'état sur le panneau, titre et icône dans la couleur d'état (comme Grafana)
    ...tint('red', '243, 84, 102', '#f35466', 0.1),
    ...tint('orange', '255, 152, 48', '#ff9830'),
    ...tint('yellow', '250, 222, 42', '#fade2a'),
    ...tint('green', '115, 191, 105', '#73bf69'),
    ...tint('blue', '87, 148, 242', '#6e9fff'),
    ...tint('gray', '204, 204, 220', '#ccccdc'),
    '--mantine-color-disabled': 'var(--mantine-color-dark-5)',
    '--mantine-color-disabled-color': 'var(--mantine-color-dark-3)',
    '--mantine-color-default': 'var(--mantine-color-dark-5)',
    '--mantine-color-default-hover': '#2a2d34',
    '--mantine-color-default-color': 'var(--mantine-color-dark-0)',
    '--app-surface': '#181b1f',
    '--app-raised': '#22252b',
    '--app-deep': '#0b0c0e',
    '--app-hover': 'rgba(204, 204, 220, 0.06)',
    '--app-border': '#2c2f35',
    '--app-muted': '#8a8b98',
    '--app-highlight': 'inset 0 1px 0 rgba(255, 255, 255, 0.06)',
    '--app-cable': '#6f707c',
    '--app-led-off': 'rgba(204, 204, 220, 0.14)',
    '--app-code-bg': '#22252b',
    '--app-tooltip-bg': '#22252b',
    '--app-tooltip-color': '#ccccdc',
    '--app-tooltip-border': '#34373e',
    '--app-focus': '#6e9fff',
    '--state-ok': '#73bf69',
    '--state-warn': '#ff9830',
    '--state-err': '#f35466',
    '--state-busy': '#fade2a',
    '--state-info': '#5794f2',
    '--state-off': '#6f707c',
  },
});

const motion = (duration: number) => ({ transitionProps: { duration, timingFunction: EASE } });

export const theme = createTheme({
  primaryColor: 'blue',
  primaryShade: { light: 6, dark: 8 },
  colors: { dark, gray, blue },
  black: '#24292e',
  white: '#ffffff',
  variantColorResolver,
  // texte noir ou blanc sur les boutons pleins selon la luminance (boutons orange des confirmations)
  autoContrast: true,
  respectReducedMotion: true,
  focusRing: 'auto',
  fontFamily: '"Inter Variable", Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
  fontFamilyMonospace: '"JetBrains Mono Variable", "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontWeights: { regular: '400', medium: '500', bold: '600' },
  headings: {
    fontWeight: '500',
    textWrap: 'pretty',
    sizes: {
      h1: { fontSize: rem(32), lineHeight: '1.2' },
      h2: { fontSize: rem(28), lineHeight: '1.2' },
      h3: { fontSize: rem(20), lineHeight: '1.3' },
      h4: { fontSize: rem(16), lineHeight: '1.4' },
      h5: { fontSize: rem(14), lineHeight: '1.4' },
      h6: { fontSize: rem(12), lineHeight: '1.4' },
    },
  },
  // grille de 4 px
  spacing: { xs: rem(8), sm: rem(12), md: rem(16), lg: rem(24), xl: rem(32) },
  // rayons serrés comme Grafana : étiquettes 4, contrôles 6, cartes et fenêtres 8
  radius: { xs: rem(4), sm: rem(6), md: rem(8), lg: rem(12), xl: rem(16) },
  defaultRadius: 'sm',
  components: {
    Text: Text.extend({ defaultProps: { size: 'sm' } }),
    // états et valeurs réseau gardent leur casse (Established, RUNNING, up) : pas de capitales forcées
    Badge: Badge.extend({
      defaultProps: { tt: 'none', radius: 'xs', variant: 'default' },
      styles: { root: { fontWeight: 500, letterSpacing: 0 } },
    }),
    Button: Button.extend({ defaultProps: { radius: 'sm' } }),
    ActionIcon: ActionIcon.extend({ defaultProps: { variant: 'subtle', color: 'gray' } }),
    Card: Card.extend({ defaultProps: { withBorder: true, radius: 'md', padding: 20 } }),
    Table: Table.extend({ defaultProps: { verticalSpacing: 8, horizontalSpacing: 12 } }),
    Tabs: Tabs.extend({ styles: { tab: { fontWeight: 500 } } }),
    AppShell: AppShell.extend({
      styles: {
        header: { backgroundColor: 'var(--app-surface)' },
        navbar: { backgroundColor: 'var(--app-surface)' },
      },
    }),
    Modal: Modal.extend({
      defaultProps: { radius: 'md', ...motion(240) },
      styles: { content: { backgroundColor: 'var(--app-surface)' }, header: { backgroundColor: 'var(--app-surface)' } },
    }),
    Drawer: Drawer.extend({
      defaultProps: motion(240),
      styles: { content: { backgroundColor: 'var(--app-surface)' }, header: { backgroundColor: 'var(--app-surface)' } },
    }),
    Menu: Menu.extend({ defaultProps: { radius: 'md', ...motion(180) } }),
    Popover: Popover.extend({ defaultProps: { radius: 'md', ...motion(180) } }),
    Tooltip: Tooltip.extend({
      defaultProps: { radius: 'sm', ...motion(180) },
      styles: {
        tooltip: {
          backgroundColor: 'var(--app-tooltip-bg)',
          color: 'var(--app-tooltip-color)',
          border: `${rem(1)} solid var(--app-tooltip-border)`,
          fontSize: rem(12),
        },
      },
    }),
  },
});
