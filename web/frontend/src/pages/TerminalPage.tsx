import { useEffect } from 'react';
import { Alert, Box, Center, Group, Loader, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useParams, useSearchParams } from 'react-router';

import { api } from '../api';
import { Terminal } from '../components/Terminal';

/** Terminal plein écran, ouvert dans une fenêtre séparée. */
export function TerminalPage() {
  const { name = '' } = useParams();
  const [params] = useSearchParams();
  const lab = useQuery({ queryKey: ['lab', 'terminal'], queryFn: api.lab, staleTime: Infinity });
  useEffect(() => {
    document.title = `${name} : terminal`;
  }, [name]);

  if (lab.isLoading) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  const card = lab.data?.nodes.find((n) => n.name === name);
  if (!card) return <Alert color="red">Nœud inconnu : {name}</Alert>;
  return (
    <Box p={8} h="100vh" className="terminal-page">
      <Group justify="space-between" mb={4} px={4}>
        <Text fw={500}>{name}</Text>
      </Group>
      <Terminal
        node={name}
        shells={card.shells}
        initialShell={params.get('shell') ?? undefined}
        height="calc(100vh - 80px)"
        popout={false}
      />
    </Box>
  );
}
