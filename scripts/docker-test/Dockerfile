# Minimal Debian image with systemd for testing scripts/biokalico-installer.sh
# (see README.md). It only adds systemd, sudo and git, so the installer must
# install everything else it needs itself, as on a bare host.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
# Makes systemd skip hardware units that would fail in a container.
ENV container=docker

RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv \
        sudo \
        git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    # Remove units that need hardware a container doesn't have.
    && rm -f /lib/systemd/system/multi-user.target.wants/* \
    && rm -f /etc/systemd/system/*.wants/* \
    && rm -f /lib/systemd/system/local-fs.target.wants/* \
    && rm -f /lib/systemd/system/sockets.target.wants/*udev* \
    && rm -f /lib/systemd/system/sockets.target.wants/*initctl* \
    && rm -f /lib/systemd/system/basic.target.wants/* \
    && rm -f /lib/systemd/system/anaconda.target.wants/* \
    # The slim image's policy-rc.d stops packages from starting their
    # services. A real host has none, so remove it.
    && rm -f /usr/sbin/policy-rc.d

RUN useradd -m -s /bin/bash biokalico \
    && echo "biokalico ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/biokalico \
    && chmod 0440 /etc/sudoers.d/biokalico

STOPSIGNAL SIGRTMIN+3

CMD ["/lib/systemd/systemd"]
