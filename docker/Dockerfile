# bty-web container.
#
# What this is: a minimal HTTP-server image hosting the bty image
# catalog + machine registry + browser UI. Operators run it as a
# trial / network-reachable image library; ``bty tui --server URL``
# clients (running from the USB live env or a workstation) connect
# here and pick images from the catalog.
#
# What this is NOT: a TFTP server. bty no longer runs any DHCP
# role (the operator's LAN DHCP server points clients at bty);
# but the appliance still bundles dnsmasq to serve the iPXE
# binaries via TFTP. This container is HTTP-only, so for
# TFTP-only PXE clients you either need a separate TFTP server
# next to the container or use the appliance image (see
# docs/walkthrough-server.md). UEFI HTTP-Boot targets work
# directly against this container.
#
# Built off the local wheel produced by ``uv build``; the GHA
# workflow stages ``dist/bty_lab-*.whl`` into the build context
# before invoking buildx, so the container's bty-web is exactly
# the version published to PyPI for the same tag.

FROM python:3.13-slim-bookworm AS runtime

# OCI image metadata. The version label is filled by the GHA
# release workflow's build-push-action via ``--label`` or by the
# Dockerfile-internal ARG below if a future build invokes ``docker
# build --build-arg BTY_VERSION=...`` directly. Other labels are
# static identity for the bty project.
ARG BTY_VERSION=dev
LABEL org.opencontainers.image.title="bty-web" \
      org.opencontainers.image.description="bty-web - HTTP server with browser UI for bty image catalog + machine registry" \
      org.opencontainers.image.source="https://github.com/safl/bty" \
      org.opencontainers.image.url="https://github.com/safl/bty" \
      org.opencontainers.image.documentation="https://safl.dk/bty" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${BTY_VERSION}"

# System deps:
#   libpam0g + libpam-modules: pamela uses pam_unix.so to verify
#     /etc/shadow on /ui/login.
#   qemu-utils: ``bty.flash.probe_image`` shells out to qemu-img
#     when bty-web inspects an uploaded image.
#   tini: PID-1 init that forwards SIGTERM cleanly so
#     ``docker stop`` doesn't fall back to SIGKILL after 10s.
#   ca-certificates: HTTPS uploads / downloads.
#   curl: HEALTHCHECK probes ``/`` so docker/orchestrators detect a
#     wedged uvicorn (Python deadlock, OOM, etc.) without needing
#     to add curl per-deployment.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpam0g \
        libpam-modules \
        qemu-utils \
        tini \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Install bty-lab[web] from the wheel staged by the workflow into
# ./dist/. The wheel is platform-independent (pure Python), so
# the same dist/ works for amd64 and arm64 buildx targets.
#
# The two-step shell-glob expansion is deliberate: pip parses
# ``bty_lab-*-py3-none-any.whl[web]`` as a literal filename + extra
# spec, but in /bin/sh the trailing ``[web]`` is a glob bracket
# pattern (any one of w/e/b), which doesn't match anything and
# leaves pip with the literal ``*`` in the filename. Resolve the
# wheel name first, then concatenate the ``[web]`` extra.
COPY dist/bty_lab-*-py3-none-any.whl /tmp/wheels/
RUN set -eu; \
    WHL=$(ls /tmp/wheels/bty_lab-*-py3-none-any.whl | head -1); \
    pip install --no-cache-dir "${WHL}[web]"; \
    rm -rf /tmp/wheels

# Default credentials: ``bty / bty``. PAM auth gates ``/ui/login``;
# the cooked image needs a known password the operator can use the
# moment the container is up. Same model the appliance uses; same
# rotation responsibility (rotate before exposing).
#
# The bty user runs the bty-web process at runtime (see USER below).
# pamela's auth path goes through pam_unix -> the setgid-shadow
# ``unix_chkpwd`` binary, which can read /etc/shadow without bty-
# web itself having shadow-group access. Same pattern the appliance
# uses (``User=bty`` in bty-web.service); aligning the container
# avoids divergent privilege levels between deployment shapes.
RUN useradd --system --no-create-home --shell /usr/sbin/nologin bty \
 && echo 'bty:bty' | chpasswd

# State + image directories. Operator volume-mounts ``/var/lib/bty``;
# bty-web writes ``state.db``, ``session-secret``, and the image
# catalog under that root. Owned by bty so the unprivileged service
# user can write without needing chown at every container start.
RUN install -d -o bty -g bty -m 0750 /var/lib/bty /var/lib/bty/images
VOLUME /var/lib/bty

# bty-web reads these env vars; defaults match the on-image
# directory layout above. ``BTY_BOOT_DIR`` is set explicitly (the
# code would default it to ``state_dir/boot`` anyway, but spelling
# it out keeps the container's env in sync with the appliance's
# ``/etc/default/bty-web`` so drift is harder to introduce.)
ENV BTY_STATE_DIR=/var/lib/bty \
    BTY_IMAGE_ROOT=/var/lib/bty/images \
    BTY_BOOT_DIR=/var/lib/bty/boot \
    BTY_WEB_HOST=0.0.0.0 \
    BTY_WEB_PORT=8080

EXPOSE 8080

# Purpose-built health endpoint. Lighter than probing ``/`` (no
# template rendering, no redirect) and explicit about its intent.
# 30s start-period covers cold-start (Python import + sqlite
# migrations); after that, probe every 30s, 5s timeout, three
# failures = unhealthy. Catches a wedged uvicorn or a process
# that crashed with the port still held.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz -o /dev/null || exit 1

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

# Run as the unprivileged bty user (matches the appliance's
# ``User=bty`` in bty-web.service). PAM authentication still works:
# pam_unix delegates to the setgid-shadow ``unix_chkpwd`` binary
# which reads /etc/shadow on bty-web's behalf. tini reaps zombies
# and forwards signals so SIGTERM lands on bty-web within ~100ms
# instead of the 10s docker-stop timeout.
USER bty
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
