# Dépôts GitLab exportés

Ce dossier contient une copie de tous les projets du GitLab du lab. Le service `gitlab-sync` la tient
à jour (voir `docker-compose.yml` et `gitlab/sync/gitlab_sync.py`).

**Il est versionné avec le projet** : quiconque clone le dépôt et lance `make up` retrouve ces projets
dans son GitLab. Le dépôt contient ceux de l'équipe, `lab-infra` (infra du lab de base en GitOps) et
`network-config` (CI/CD réseau avec Ansible).

Un bundle contient **tout l'historique** du projet, branches comprises : relisez ce qu'un export
apporte (`git diff --stat gitlab/repos/`) avant de le committer, surtout dans un dépôt public.

## Format

Chaque projet GitLab est rangé sous le chemin de son namespace, groupe ou utilisateur :

```
<groupe>/
  ansible-sonic.json      nom, description, visibilité, branche par défaut, refs (branches, tags)
  ansible-sonic.bundle    toutes les branches et tous les tags (git bundle) ; absent si le dépôt est vide
<utilisateur>/
  essais.json
  essais.bundle
```

Un bundle se clone comme un dépôt distant :
`git clone gitlab/repos/<groupe>/ansible-sonic.bundle`.

## Fonctionnement

Le service compare GitLab et ce dossier au démarrage, puis toutes les `GITLAB_SYNC_INTERVAL`
secondes (60 par défaut). `make gitlab-sync` lance un passage immédiat. Un dernier export a lieu à
l'arrêt du service.

- **Projet créé ou modifié dans GitLab** (push, nouvelle branche, tag, description…) : ses fichiers
  sont réécrits.
- **GitLab neuf** (clone du dépôt sur un autre poste, volumes supprimés) : les projets
  absents sont recréés, avec leur groupe, leurs branches, leurs tags et leur branche par défaut.
- **Fichiers modifiés par un `git pull`** du dépôt : les nouveautés sont poussées dans
  GitLab (branches en avance, nouvelles branches, nouveaux tags). Si une branche a divergé entre ce
  dossier et GitLab, la version de GitLab est conservée et un avertissement apparaît dans
  `docker compose logs gitlab-sync`. L'autre version reste dans l'historique git de ce dépôt.
- **Projet supprimé dans GitLab** : ses fichiers sont supprimés. Supprimer un fichier ne supprime
  pas le projet : il est réexporté au passage suivant.

## Limites

- Seul le contenu git (branches, tags) voyage, avec le nom, la description, la visibilité et la
  branche par défaut. Ne voyagent pas : les issues, les merge requests, le wiki, les variables CI et
  les membres des groupes autres que celui du `.env`.
- Un projet personnel n'est restauré que si son propriétaire figure dans `GITLAB_USERS`.
- Les fichiers `*.tmp` sont des écritures en cours, ignorés par git.
