"""Agent GitOps du lab : la topologie et les confs de démarrage suivent un projet GitLab.

S'exécute dans le service compose « clab » (containerlab, socket Docker), lancé par lab/clab-runner.sh :
  - pour chaque merge request ouverte : plan containerlab (--dry-run), commentaire et statut lab/plan ;
  - à chaque commit de la branche par défaut : installation des fichiers dans lab/, réconciliation du
    lab (containerlab deploy), recréation des nœuds dont la définition a changé, restauration des confs
    de démarrage modifiées, vérification, retour en arrière en cas d'échec ; statut lab/deploy.

Modules purs (testés sans containerlab ni GitLab) : validate (aussi copié dans le projet GitLab pour la
CI), plan. Entrées/sorties : gitlab (API), agent (boucle, commandes, fichiers).
"""
