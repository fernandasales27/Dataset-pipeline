# Imagem do experimento Babel (nome da imagem: mflos_babel, prefixo exigido pelo servidor).
FROM eclipse-temurin:21-jdk-jammy AS java
FROM node:22-bookworm-slim AS node

FROM alpine:3.20 AS pmd
RUN apk add --no-cache curl unzip
# Versoes do PMD tentadas (da mais nova pra mais antiga) quando o ruleset do projeto nao carrega.
ARG PMD_VERSIONS="5.8.1 6.0.1 6.10.0 6.21.0 6.34.0 6.55.0 7.0.0 7.9.0 7.29.0"
RUN set -e; mkdir -p /opt/pmd; cd /opt/pmd; \
    for v in $PMD_VERSIONS; do \
      case "$v" in 7.*) f="pmd-dist-$v-bin.zip";; *) f="pmd-bin-$v.zip";; esac; \
      curl -fsSL -o "$f" "https://github.com/pmd/pmd/releases/download/pmd_releases%2F$v/$f"; \
      unzip -q "$f"; rm "$f"; \
    done; ls /opt/pmd

FROM python:3.10-slim-bookworm

# git e certificados: o run_linters.py faz checkout dos commits e acessa o GitHub
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Java 21
COPY --from=java /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# Node 22 + npm + ESLint 9.39.5
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && npm install -g eslint@9.39.5

# Python (pylint 4.0.10 e dependencias fixadas)
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Checkstyle: 8 versoes (jars copiados da pasta jars/, que nao vai para o git)
COPY jars/ /opt/checkstyle/

# PMD: uma pasta pmd-bin-X.Y.Z por versao
COPY --from=pmd /opt/pmd /opt/pmd

# Scripts do experimento (por ultimo, para rebuild rapido quando o codigo mudar)
COPY *.py /app/

WORKDIR /data
