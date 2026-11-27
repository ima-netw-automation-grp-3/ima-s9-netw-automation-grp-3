"""python3 -m gitops : lance l'agent GitOps (depuis le dossier lab/, lancé par clab-runner.sh)."""

from .agent import main

raise SystemExit(main())
