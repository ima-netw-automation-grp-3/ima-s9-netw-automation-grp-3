import CodeMirror from '@uiw/react-codemirror';
import { json } from '@codemirror/lang-json';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { EditorView } from '@codemirror/view';
import { tags as t } from '@lezer/highlight';
import { useComputedColorScheme } from '@mantine/core';

interface Props {
  value: string;
  onChange?: (value: string) => void;
  language?: 'json' | 'text';
  height?: number;
  readOnly?: boolean;
}

const JSON_EXT = [json()];
const NO_EXT: never[] = [];

/** Éditeur dans la rampe de l'interface : fond profond, gouttière discrète, curseur et sélection au bleu des liens. */
const spec = {
  '&': { backgroundColor: 'var(--app-deep)', color: 'var(--mantine-color-text)' },
  '.cm-scroller': { fontFamily: 'var(--mantine-font-family-monospace)', lineHeight: '1.5' },
  '.cm-content': { caretColor: 'var(--app-focus)' },
  '&.cm-focused': { outline: 'none' },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--app-focus)' },
  '&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection': {
    backgroundColor: 'color-mix(in srgb, var(--app-focus) 28%, transparent)',
  },
  '.cm-gutters': { backgroundColor: 'var(--app-deep)', color: 'var(--app-muted)', borderRight: '1px solid var(--app-border)' },
  '.cm-activeLine': { backgroundColor: 'var(--app-hover)' },
  '.cm-activeLineGutter': { backgroundColor: 'var(--app-hover)', color: 'var(--mantine-color-text)' },
  '.cm-foldPlaceholder': {
    backgroundColor: 'var(--app-raised)',
    border: '1px solid var(--app-border)',
    color: 'var(--mantine-color-dimmed)',
  },
  '&.cm-focused .cm-matchingBracket': { backgroundColor: 'color-mix(in srgb, var(--app-focus) 22%, transparent)' },
};

// clés au bleu des liens, chaînes au vert des états, nombres et booléens à l'ambre, ponctuation discrète
const highlight = syntaxHighlighting(
  HighlightStyle.define([
    { tag: t.propertyName, color: 'var(--mantine-color-anchor)' },
    { tag: t.string, color: 'var(--state-ok)' },
    { tag: [t.number, t.bool, t.null], color: 'var(--state-warn)' },
    { tag: [t.punctuation, t.separator, t.brace, t.squareBracket], color: 'var(--app-muted)' },
    { tag: [t.comment, t.lineComment], color: 'var(--app-muted)' },
  ]),
);

const DARK = [EditorView.theme(spec, { dark: true }), highlight];
const LIGHT = [EditorView.theme(spec, { dark: false }), highlight];

export function CodeEditor({ value, onChange, language = 'text', height = 520, readOnly = false }: Props) {
  const scheme = useComputedColorScheme('light');
  return (
    <CodeMirror
      value={value}
      onChange={onChange}
      extensions={language === 'json' ? JSON_EXT : NO_EXT}
      height={`${height}px`}
      theme={scheme === 'dark' ? DARK : LIGHT}
      readOnly={readOnly}
      basicSetup={{ foldGutter: true, highlightActiveLine: !readOnly, autocompletion: false }}
      className="code-editor"
    />
  );
}
