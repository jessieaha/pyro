# syntax=docker/dockerfile:1.7
# One image for Codespaces / Cursor / any VM (Docker or Podman) and, converted
# with Apptainer, for Snellius and VU ADA. Python deps come from uv.lock.
#
#   docker build -t pyro:dev --target dev .
#   docker build -t pyro:latest .
#   apptainer build pyro.sif docker://ghcr.io/jessieaha/pyro:latest

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_VERSION=0.13.7

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM ${PYTHON_IMAGE} AS runtime
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYRO_SYNC_CONFIG=/opt/pyro/config/sync.toml

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git openssh-client \
      cdo nco netcdf-bin rclone \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /opt/pyro

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --all-extras

COPY src ./src
COPY config ./config
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --all-extras \
 && python -m ipykernel install --prefix /opt/venv --name pyro --display-name "pyro (container)" \
 && chmod -R a+rX /opt/venv /opt/pyro

# HPC runtimes run as the calling user; keep the image usable by any UID.
RUN useradd --create-home --uid 1000 pyro
USER pyro
WORKDIR /work
CMD ["bash"]

FROM runtime AS dev
USER root
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /opt/pyro && uv sync --frozen --all-extras \
 && chmod -R a+rX /opt/venv
USER pyro
