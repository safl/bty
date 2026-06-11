# bty-web container: a plain HTTP app server (UI + PXE plans + boot
# artifacts + image registry). No TFTP, no DHCP -- the operator's LAN
# DHCP points clients here, and the compose `tftp` sidecar serves the
# iPXE NBP over TFTP for BIOS/legacy clients. UEFI HTTP-Boot fetches
# ipxe.efi straight from this server over HTTP.
#
# Built off the local wheel produced by ``uv build``; the GHA workflow
# stages ``dist/bty_lab-*.whl`` into the build context before invoking
# buildx, so the container's bty-web is exactly the version published to
# PyPI for the same tag.

FROM python:3.13-slim-bookworm AS runtime

# OCI image metadata. The version label is filled by the GHA release
# workflow's build-push-action via ``--label`` or by ``docker build
# --build-arg BTY_VERSION=...``.
ARG BTY_VERSION=dev
LABEL org.opencontainers.image.title="bty-web" \
      org.opencontainers.image.description="bty-web - HTTP server with browser UI for bty image catalog + machine registry" \
      org.opencontainers.image.source="https://github.com/safl/bty" \
      org.opencontainers.image.url="https://github.com/safl/bty" \
      org.opencontainers.image.documentation="https://safl.dk/bty" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${BTY_VERSION}"

# Runtime deps, kept minimal:
#   qemu-utils:      ``bty.flash.probe_image`` shells out to qemu-img
#                    when bty-web inspects an uploaded image.
#   ca-certificates: HTTPS uploads / downloads (oras:// + http(s):// are
#                    fetched in-process via urllib).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        qemu-utils \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install bty-lab[web] from the wheel staged by the workflow into
# ./dist/. The wheel is pure Python, so the same dist/ works for amd64
# and arm64 buildx targets.
#
# Two-step glob: pip parses ``bty_lab-*.whl[web]`` as a literal filename
# + extra, but /bin/sh treats trailing ``[web]`` as a glob bracket, so
# resolve the wheel name first then append the extra. ``sort -V | tail
# -1`` picks the highest version when dist/ holds several (``uv build``
# appends rather than wipes).
COPY dist/bty_lab-*-py3-none-any.whl /tmp/wheels/
RUN set -eu; \
    WHL=$(ls /tmp/wheels/bty_lab-*-py3-none-any.whl | sort -V | tail -1); \
    pip install --no-cache-dir "${WHL}[web]"; \
    rm -rf /tmp/wheels

# Baked bootstrap artifacts. CI drops bty's custom ipxe.efi into
# ``docker/seed/`` before the build; dev builds leave just the .gitkeep
# placeholder. bty-web copies any real file from BTY_BOOT_SEED_DIR into
# BTY_BOOT_DIR on startup (when absent), so UEFI HTTP-Boot serves bty's
# embedded-chain iPXE out of the box.
COPY docker/seed/ /usr/share/bty/boot-seed/

# Run as an unprivileged user. ``--uid 1000`` is pinned so an operator's
# host-side ``chown -R 1000:1000 ./bty-data`` matches across rebuilds.
RUN groupadd --gid 1000 bty \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin bty \
 && install -d -o bty -g bty -m 0750 /var/lib/bty /var/lib/bty/boot
VOLUME /var/lib/bty

# bty-web reads these; defaults match the on-image layout. v0.45
# canonical names: BTY_<SECTION>_<KEY> per the bty.toml schema (the
# v0.41 flat aliases were removed). The operator UI is gated by
# ``$BTY_ADMIN_PASSWORD`` (set at run time; unset leaves it open
# with a startup warning).
ENV BTY_PATHS_STATE_DIR=/var/lib/bty \
    BTY_PATHS_BOOT_DIR=/var/lib/bty/boot \
    BTY_BOOT_SEED_DIR=/usr/share/bty/boot-seed \
    BTY_SERVER_HOST=0.0.0.0 \
    BTY_SERVER_PORT=8080

EXPOSE 8080

# Purpose-built health endpoint, probed in-process (no curl dep). 30s
# start-period covers cold-start (imports + sqlite migrations). The
# probe reads BTY_SERVER_PORT so an operator override
# (``docker run -e BTY_SERVER_PORT=9000``) keeps the healthcheck
# pointed at the right port instead of the stale Dockerfile default.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; p=os.environ.get('BTY_SERVER_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz',timeout=3).status==200 else 1)"]

USER bty
ENTRYPOINT ["bty-web"]
