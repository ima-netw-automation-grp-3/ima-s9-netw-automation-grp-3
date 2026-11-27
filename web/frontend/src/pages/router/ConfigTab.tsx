import { useEffect, useState, type ReactNode } from 'react';
import {
  Accordion,
  ActionIcon,
  Alert,
  Anchor,
  Button,
  Card,
  Code,
  FileButton,
  Grid,
  Group,
  List,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { modals } from '@mantine/modals';
import {
  IconAlertTriangle,
  IconCloudDownload,
  IconDeviceFloppy,
  IconEye,
  IconFileDownload,
  IconSend,
  IconUpload,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '../../api';
import { CodeEditor } from '../../components/CodeEditor';
import { ConfigDiffModal } from '../../components/ConfigDiff';
import { ChangesTable, DiffView, Led, OutputBlock, SummaryLine } from '../../components/common';
import type { Domain, Preview, PushMode, PushResult } from '../../types';
import { useLab } from '../../hooks';
import { downloadText, notifyError, notifySave, notifySuccess, saveTarget } from '../../utils';

/** Sous-titre d'un bloc de l'aperçu ou du résultat. */
function Label({ children }: { children: ReactNode }) {
  return (
    <Text size="sm" fw={500}>
      {children}
    </Text>
  );
}

const DOMAIN_LABEL: Record<Domain, string> = { configdb: 'CONFIG_DB', frr: 'FRR' };
const FILE_NAME: Record<Domain, string> = { configdb: 'config_db.json', frr: 'frr.conf' };

function Help({ domain }: { domain: Domain }) {
  if (domain === 'configdb') {
    return (
      <Stack gap="xs">
        <Text size="sm">
          <b>Merge</b> : les clés et champs fournis sont ajoutés ou mis à jour, rien n'est supprimé (équivalent de{' '}
          <Code>config load</Code>). Un extrait suffit, par exemple :
        </Text>
        <Code block>{'{\n  "PORT": { "Ethernet4": { "admin_status": "down" } }\n}'}</Code>
        <Text size="sm">
          <b>Replace</b> : l'éditeur fait foi. Les clés absentes sont supprimées, sauf dans les tables système (PORT,
          DEVICE_METADATA, buffers/QoS…), qui sont seulement fusionnées. À utiliser avec une conf complète.
        </Text>
        <Text size="sm" c="dimmed">
          Les changements sont écrits dans CONFIG_DB ; orchagent, intfmgrd, etc. les appliquent aussitôt. Cliquez sur
          « Aperçu » pour voir les opérations avant de pousser.
        </Text>
      </Stack>
    );
  }
  return (
    <Stack gap="xs">
      <Text size="sm">
        <b>Merge</b> : la conf est appliquée avec <Code>vtysh -f</Code>. Les lignes sont ajoutées ou modifiées ; pour
        retirer quelque chose, écrivez <Code>no …</Code>.
      </Text>
      <Text size="sm">
        <b>Replace</b> : ce qui n'est pas dans l'éditeur est retiré. Un bloc <Code>router bgp</Code> dont des lignes
        disparaissent est supprimé puis recréé, ce qui réinitialise ses sessions BGP.
      </Text>
      <Text size="sm" c="dimmed">
        La syntaxe est vérifiée (<Code>vtysh -C</Code>) avant tout push. Les lignes gérées par SONiC (fpm, nht, route-map
        RM_SET_SRC) sont masquées et jamais retirées.
      </Text>
    </Stack>
  );
}

function PreviewView({ preview }: { preview: Preview }) {
  if (preview.domain === 'configdb') {
    return (
      <Stack gap="sm">
        <SummaryLine summary={preview.summary} />
        <ChangesTable changes={preview.changes} />
        {preview.diff && (
          <Accordion variant="contained" radius="sm">
            <Accordion.Item value="diff">
              <Accordion.Control>Diff JSON (running → après push)</Accordion.Control>
              <Accordion.Panel>
                <DiffView diff={preview.diff} />
              </Accordion.Panel>
            </Accordion.Item>
          </Accordion>
        )}
      </Stack>
    );
  }
  return (
    <Stack gap="sm">
      {!preview.valid && (
        <Alert color="red" title="Syntaxe FRR invalide" icon={<IconAlertTriangle size={16} />}>
          <Code block>{preview.errors}</Code>
        </Alert>
      )}
      {preview.mode === 'replace' && preview.replace && (
        <>
          {preview.replace.resets.length > 0 && (
            <Alert color="orange" title="Blocs recréés (sessions réinitialisées)">
              <List size="sm">
                {preview.replace.resets.map((r) => (
                  <List.Item key={r}>
                    <Code>{r}</Code>
                  </List.Item>
                ))}
              </List>
            </Alert>
          )}
          <Label>Commandes de retrait exécutées avant l'application</Label>
          <OutputBlock text={preview.replace.commands.join('\n')} empty="(aucun retrait)" maxHeight={180} />
        </>
      )}
      {preview.mode === 'merge' && (
        <>
          <Label>Lignes nouvelles ou modifiées par le merge</Label>
          <OutputBlock text={preview.additions ?? ''} empty="(rien de nouveau : la conf est déjà présente)" maxHeight={200} />
        </>
      )}
      <Label>
        Diff running → conf de l'éditeur{' '}
        {preview.mode === 'merge' && (
          <Text span size="xs" c="dimmed" fw={400}>
            (en merge, les lignes « - » ne sont pas retirées)
          </Text>
        )}
      </Label>
      <DiffView diff={preview.diff} />
    </Stack>
  );
}

function ResultView({ result }: { result: PushResult }) {
  return (
    <Stack gap="sm">
      {result.ok ? (
        <div>
          <span className="state-label">
            <Led tone="ok" />
            <Text span fw={500}>
              Configuration appliquée
            </Text>
          </span>
          {result.message && (
            <Text size="sm" c="dimmed" mt={4}>
              {result.message}
            </Text>
          )}
        </div>
      ) : (
        <Alert color="red" title="Application partielle">
          {result.message}
        </Alert>
      )}
      {result.domain === 'configdb' ? (
        <>
          <SummaryLine summary={result.summary} />
          <ChangesTable changes={result.changes} />
          {(result.failed?.length ?? 0) > 0 && (
            <Alert color="red" title="Opérations en échec">
              {result.failed!.map((f) => (
                <Text key={`${f.table}-${f.key}`} size="xs" ff="monospace">
                  {f.op} {f.table}|{f.key} : {f.error}
                </Text>
              ))}
            </Alert>
          )}
        </>
      ) : (
        <>
          {(result.resets?.length ?? 0) > 0 && (
            <Alert color="orange">Blocs recréés : {result.resets!.join(', ')}</Alert>
          )}
          {result.steps
            ?.filter((s) => s.output || s.exit_code !== 0)
            .map((s) =>
              s.exit_code === 0 ? (
                <Stack key={s.step} gap={4}>
                  <Label>
                    Étape « {s.step} »{' '}
                    <Text span size="xs" className="mono muted" fw={400}>
                      code 0
                    </Text>
                  </Label>
                  <OutputBlock text={s.output} maxHeight={200} />
                </Stack>
              ) : (
                <Alert key={s.step} color="red" title={`Étape « ${s.step} » (code ${s.exit_code})`}>
                  <Code block>{s.output || '(pas de sortie)'}</Code>
                </Alert>
              ),
            )}
          <Label>Changements effectifs (running avant → après)</Label>
          <DiffView diff={result.diff} empty="Aucun changement effectif" />
        </>
      )}
    </Stack>
  );
}

export function ConfigTab({ name }: { name: string }) {
  const qc = useQueryClient();
  const { data: lab } = useLab();
  const [domain, setDomain] = useState<Domain>('configdb');
  const [buffers, setBuffers] = useState<Record<Domain, string>>({ configdb: '', frr: '' });
  const [loaded, setLoaded] = useState<Record<Domain, { source: string; content: string } | null>>({
    configdb: null,
    frr: null,
  });
  const [source, setSource] = useState('running');
  const [mode, setMode] = useState<PushMode>('merge');
  const [preview, setPreview] = useState<Record<Domain, Preview | null>>({ configdb: null, frr: null });
  const [result, setResult] = useState<Record<Domain, PushResult | null>>({ configdb: null, frr: null });
  const [rawFrr, setRawFrr] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [compare, setCompare] = useState<string | null>(null);

  const snapshots = useQuery({ queryKey: ['snapshots', name], queryFn: () => api.snapshots(name) });
  const dirty = useQuery({ queryKey: ['diff', name, 'startup'], queryFn: () => api.diff(name), refetchInterval: 15000 });

  const refreshAfterChange = () => {
    qc.invalidateQueries({ queryKey: ['diff', name] });
    qc.invalidateQueries({ queryKey: ['node', name] });
    qc.invalidateQueries({ queryKey: ['lab'] });
  };

  const load = useMutation({
    mutationFn: (v: { domain: Domain; source: string }) => api.getConfig(name, v.domain, v.source),
    onSuccess: (doc) => {
      setBuffers((b) => ({ ...b, [doc.domain]: doc.content }));
      setLoaded((l) => ({ ...l, [doc.domain]: { source: doc.source, content: doc.content } }));
      setPreview((p) => ({ ...p, [doc.domain]: null }));
      setResult((r) => ({ ...r, [doc.domain]: null }));
      if (doc.domain === 'frr' && doc.raw) setRawFrr(doc.raw);
    },
    onError: (e) => notifyError(e, 'Récupération de la configuration'),
  });

  useEffect(() => {
    if (loaded[domain] === null) load.mutate({ domain, source: 'running' });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domain]);

  const doPreview = useMutation({
    mutationFn: () => api.preview(name, domain, mode, buffers[domain]),
    onSuccess: (p) => {
      setPreview((x) => ({ ...x, [domain]: p }));
      setResult((r) => ({ ...r, [domain]: null }));
    },
    onError: (e) => notifyError(e, 'Aperçu'),
  });

  const push = useMutation({
    mutationFn: () => api.push(name, domain, mode, buffers[domain]),
    onSuccess: (r) => {
      setResult((x) => ({ ...x, [domain]: r }));
      setPreview((x) => ({ ...x, [domain]: null }));
      refreshAfterChange();
      if (r.ok) notifySuccess(r.message ?? 'Configuration appliquée', `Push ${DOMAIN_LABEL[domain]}`);
      else notifyError(new Error(r.message ?? 'certaines opérations ont échoué'), `Push ${DOMAIN_LABEL[domain]}`);
    },
    onError: (e) => notifyError(e, `Push ${DOMAIN_LABEL[domain]}`),
  });

  const save = useMutation({
    mutationFn: () => api.save(name),
    onSuccess: (r) => {
      notifySave(r, 'Sauvegarde');
      qc.invalidateQueries({ queryKey: ['diff', name] });
    },
    onError: (e) => notifyError(e, 'Sauvegarde'),
  });

  const confirmPush = () => {
    if (mode === 'merge') {
      push.mutate();
      return;
    }
    modals.openConfirmModal({
      title: `Pousser ${DOMAIN_LABEL[domain]} en mode replace`,
      children: (
        <Text size="sm">
          La conf de l'éditeur remplace la conf {DOMAIN_LABEL[domain]} running de <b>{name}</b> : ce qui n'y figure pas
          sera retiré
          {domain === 'frr' ? ' (blocs router modifiés recréés : sessions BGP réinitialisées).' : ' (sauf tables système).'}
        </Text>
      ),
      labels: { confirm: 'Pousser', cancel: 'Annuler' },
      confirmProps: { color: 'orange.6' },
      onConfirm: () => push.mutate(),
    });
  };

  const confirmSave = () =>
    modals.openConfirmModal({
      title: 'Sauvegarder comme conf de démarrage',
      children: (
        <Text size="sm">
          {saveTarget(lab?.gitops, `La conf running de ${name} (CONFIG_DB et FRR, pas le contenu de l'éditeur)`)}
        </Text>
      ),
      labels: { confirm: 'Sauvegarder', cancel: 'Annuler' },
      onConfirm: () => save.mutate(),
    });

  const sources = [
    { value: 'running', label: 'Running (routeur)' },
    { value: 'startup', label: 'Conf de démarrage (lab/configs)' },
    ...(snapshots.data?.snapshots ?? [])
      .filter((s) => s.domains.includes(domain))
      .map((s) => ({ value: `snapshot:${s.id}`, label: `Snapshot ${s.label ? `« ${s.label} »` : s.id}` })),
  ];
  const current = loaded[domain];
  const modified = current !== null && buffers[domain] !== current.content;

  return (
    <Stack gap="md">
      <Group justify="space-between" gap="sm">
        <Group gap="md">
          <SegmentedControl
            value={domain}
            onChange={(v) => setDomain(v as Domain)}
            data={[
              { value: 'configdb', label: 'CONFIG_DB (SONiC)' },
              { value: 'frr', label: 'FRR (routage)' },
            ]}
          />
          {dirty.data &&
            (dirty.data.dirty ? (
              <Tooltip label="Différences entre la running et la conf de démarrage : cliquer pour voir">
                <Anchor component="button" type="button" size="sm" c="var(--state-warn)" onClick={() => setCompare('startup')}>
                  <span className="state-label">
                    <Led tone="warn" />
                    modifications non sauvegardées
                  </span>
                </Anchor>
              </Tooltip>
            ) : (
              <span className="state-label">
                <Led tone="ok" />
                <Text span size="sm" c="dimmed">
                  running = conf de démarrage
                </Text>
              </span>
            ))}
        </Group>
        <Button variant="default" leftSection={<IconDeviceFloppy size={16} />} loading={save.isPending} onClick={confirmSave}>
          Sauvegarder running → startup
        </Button>
      </Group>

      <Grid>
        <Grid.Col span={{ base: 12, lg: 7 }}>
          <Card padding="md">
            <Group justify="space-between" mb="xs" wrap="wrap" gap="xs">
              <Group gap="xs">
                <Select size="xs" w={300} data={sources} value={source} onChange={(v) => setSource(v ?? 'running')} allowDeselect={false} />
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconCloudDownload size={16} />}
                  loading={load.isPending}
                  onClick={() => load.mutate({ domain, source })}
                >
                  Récupérer
                </Button>
              </Group>
              <Group gap={4}>
                <FileButton
                  accept=".json,.conf,.txt,text/plain,application/json"
                  onChange={(file) => file?.text().then((t) => setBuffers((b) => ({ ...b, [domain]: t })))}
                >
                  {(props) => (
                    <Tooltip label="Importer un fichier dans l'éditeur">
                      <ActionIcon size={30} aria-label="Importer un fichier dans l'éditeur" {...props}>
                        <IconUpload size={16} />
                      </ActionIcon>
                    </Tooltip>
                  )}
                </FileButton>
                <Tooltip label="Télécharger le contenu de l'éditeur">
                  <ActionIcon
                    size={30}
                    aria-label="Télécharger le contenu de l'éditeur"
                    onClick={() =>
                      downloadText(`${name}-${FILE_NAME[domain]}`, buffers[domain], domain === 'frr' ? 'text/plain' : 'application/json')
                    }
                  >
                    <IconFileDownload size={16} />
                  </ActionIcon>
                </Tooltip>
              </Group>
            </Group>
            <Text size="xs" c="dimmed" mb={8}>
              Source : <span className="mono">{current?.source ?? '—'}</span>
              {modified && (
                <Text span c="var(--state-warn)" size="xs">
                  {' '}
                  · modifié dans l'éditeur
                </Text>
              )}
            </Text>
            <CodeEditor
              value={buffers[domain]}
              onChange={(v) => setBuffers((b) => ({ ...b, [domain]: v }))}
              language={domain === 'configdb' ? 'json' : 'text'}
              height={560}
            />
            {domain === 'frr' && rawFrr && (
              <Text size="xs" c="dimmed" mt={8}>
                Lignes gérées par SONiC masquées.{' '}
                <Anchor size="xs" onClick={() => setShowRaw(true)}>
                  Voir la conf brute (show running-config)
                </Anchor>
              </Text>
            )}
            <Group justify="space-between" mt="sm" wrap="wrap" gap="xs">
              <SegmentedControl
                size="xs"
                value={mode}
                onChange={(v) => setMode(v as PushMode)}
                data={[
                  { value: 'merge', label: 'Merge (ajout / modif)' },
                  { value: 'replace', label: 'Replace (remplacement)' },
                ]}
              />
              <Group gap="xs">
                <Button variant="default" leftSection={<IconEye size={16} />} loading={doPreview.isPending} onClick={() => doPreview.mutate()}>
                  Aperçu
                </Button>
                <Button
                  leftSection={<IconSend size={16} />}
                  loading={push.isPending}
                  onClick={confirmPush}
                >
                  Pousser
                </Button>
              </Group>
            </Group>
          </Card>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 5 }}>
          <Card padding="md">
            <Text component="h3" m={0} mb="sm" fz="md" fw={500}>
              {result[domain] ? 'Résultat du push' : preview[domain] ? `Aperçu (${preview[domain]!.mode})` : `Pousser une conf ${DOMAIN_LABEL[domain]}`}
            </Text>
            {result[domain] ? (
              <ResultView result={result[domain]!} />
            ) : preview[domain] ? (
              <PreviewView preview={preview[domain]!} />
            ) : (
              <Help domain={domain} />
            )}
          </Card>
        </Grid.Col>
      </Grid>

      <ConfigDiffModal node={name} against={compare} onClose={() => setCompare(null)} />
      <Modal opened={showRaw} onClose={() => setShowRaw(false)} size="xl" title={`${name} : show running-config`}>
        <OutputBlock text={rawFrr ?? ''} maxHeight={600} />
      </Modal>
    </Stack>
  );
}
