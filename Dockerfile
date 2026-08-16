# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Builder stage: install dependencies + the package into /wheels.
# ---------------------------------------------------------------------------
# Pinned by digest so rebuilds are reproducible. Refresh with:
#   docker pull python:3.13-slim
#   docker inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
# Dependabot keeps it fresh weekly via .github/dependabot.yml.
#
# The TAG must stay 3.13: pyproject.toml (requires-python, mypy python_version,
# ruff target-version), the CI matrix, and requirements.lock (compiled with
# --python-version 3.13) all target 3.13. Moving the tag alone silently ships a
# runtime that no lockfile or check ever exercised. Retarget all of them
# together or not at all.
#
# This already happened once: a Dependabot *digest* bump carried the tag from
# 3.13 to 3.14 and shipped Python 3.14.7 to production, dropping the digest pin
# entirely on the way through. A digest is opaque, so the tag beside it gets
# edited without reading as a version change. CI now asserts the built image's
# Python minor version (see the build-image job in .github/workflows/ci.yml),
# so that failure mode is loud rather than silent. If Dependabot proposes a
# FROM line whose tag is not 3.13, close the PR.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS builder

WORKDIR /build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install from the hash-locked requirements.lock (uv pip compile --generate-hashes).
# --require-hashes refuses anything that doesn't match a pinned sha256.
COPY requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes --target /wheels -r requirements.lock

COPY pyproject.toml README.md ./
COPY src/ ./src/
# Install the package itself (deps already satisfied above) into the same tree.
RUN pip install --no-cache-dir --target /wheels --no-deps .

# ---------------------------------------------------------------------------
# Runtime stage: slim image with only the installed package + UID 1000 user.
# ---------------------------------------------------------------------------
# Same pin as the builder stage. Keep both stages on the identical tag+digest.
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/site-packages \
    PATH=/app/site-packages/bin:$PATH

# Apply current Debian security updates on top of the pinned Python base image.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root user with pinned UID 1000 (no shell, no home).
RUN groupadd --system --gid 1000 mcp \
    && useradd --system --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin mcp \
    && mkdir -p /data \
    && chown -R mcp:mcp /data

# Drop pip from the runtime image. Nothing at runtime uses it: dependencies are built
# in the builder stage and reach this stage via PYTHONPATH, and the entrypoint
# and healthcheck are plain `python -m` calls.
#
# This is also the only fix for two recurring Trivy HIGHs. pip ships a vendored
# dependency set (see pip/_vendor/vendor.txt) that Trivy scans as real packages:
# msgpack 1.1.2 (GHSA-6v7p-g79w-8964) and setuptools 70.3.0 (CVE-2025-47273).
# Neither is an application dependency, so no lockfile change can move them, and
# no pip release ships fixed versions. Removing the unused component is the fix.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.*/site-packages/pip \
              /usr/local/lib/python3.*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

WORKDIR /app
COPY --from=builder /wheels /app/site-packages
RUN chown -R mcp:mcp /app

USER mcp

EXPOSE 3717

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD ["python", "-m", "mcp_umphreys.healthcheck"]

ENTRYPOINT ["python", "-m", "mcp_umphreys.server"]
