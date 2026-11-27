# syntax=docker/dockerfile:1
# Service compose « clab » : containerlab, plus Python pour l'agent GitOps (lab/gitops/, lancé par
# lab/clab-runner.sh depuis le projet monté). L'image d'origine est une Alpine sans Python.

FROM ghcr.io/srl-labs/clab:0.83.0
RUN apk add --no-cache python3 py3-yaml
