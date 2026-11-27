# syntax=docker/dockerfile:1
# Runner GitLab du lab : image officielle (même version que GitLab) + enregistrement automatique.
# Voir entrypoint.sh ; les jobs tournent dans des conteneurs (executor docker) sur le réseau de
# management du lab.

FROM gitlab/gitlab-runner:alpine-v19.6.0
RUN apk add --no-cache jq
COPY --chmod=755 entrypoint.sh /usr/local/bin/sonic-lab-runner
ENTRYPOINT ["/usr/bin/dumb-init", "/usr/local/bin/sonic-lab-runner"]
CMD ["run", "--user=gitlab-runner", "--working-directory=/home/gitlab-runner"]
