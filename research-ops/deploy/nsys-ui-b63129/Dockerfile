FROM registry.example.com/acg-agi/slime:v0.2.4

ARG NSYS_VERSION=2026.2.1.210-3763964
ARG NSYS_URL=https://developer.nvidia.com/downloads/assets/tools/secure/nsight-systems/2026_2/NsightSystems-linux-public-${NSYS_VERSION}.run

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        dbus-x11 \
        findutils \
        fonts-dejavu-core \
        libasound2t64 \
        libegl1 \
        libfontconfig1 \
        libfreetype6 \
        libgl1 \
        libgl1-mesa-dri \
        libglu1-mesa \
        libice6 \
        libnss3 \
        libsm6 \
        libx11-6 \
        libx11-xcb1 \
        libxcb-cursor0 \
        libxcb-dri2-0 \
        libxcb-icccm4 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        libxcb-shape0 \
        libxcb-sync1 \
        libxcb-util1 \
        libxcb-xfixes0 \
        libxcb-xinerama0 \
        libxcb-xinput0 \
        libxcb-xkb1 \
        libxext6 \
        libxi6 \
        libxkbcommon-x11-0 \
        libxkbfile1 \
        libxrandr2 \
        libxrender1 \
        libxtst6 \
        novnc \
        openbox \
        procps \
        tini \
        websockify \
        x11-utils \
        x11vnc \
        xterm \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fL --retry 3 --retry-delay 2 -o /tmp/nsight-systems.run "${NSYS_URL}" \
    && chmod +x /tmp/nsight-systems.run \
    && /tmp/nsight-systems.run --accept --quiet -- \
        -noprompt -targetpath=/opt/nvidia/nsight-systems/2026.2.1 \
    && rm -f /tmp/nsight-systems.run \
    && chmod 0755 /opt/nvidia/nsight-systems \
    && chmod -R go-w /opt/nvidia/nsight-systems/2026.2.1 \
    && NSYS_UI="$(find /opt/nvidia/nsight-systems -type f -path '*/host-linux-x64/nsys-ui' -print -quit)" \
    && test -x "${NSYS_UI}" \
    && ln -s "${NSYS_UI}" /usr/local/bin/nsys-ui

RUN apt-get update \
    && apt-get install -y --no-install-recommends libwayland-server0 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash viewer \
    && install -d -o viewer -g viewer /rank-reports \
    && install -d -o viewer -g viewer "/home/viewer/.config/NVIDIA Corporation"

COPY --chown=viewer:viewer start.sh /usr/local/bin/start-nsys-ui
COPY --chown=viewer:viewer ["nsys-settings.ini", "/home/viewer/.config/NVIDIA Corporation/NVIDIA Nsight Systems.ini"]
RUN chmod 0755 /usr/local/bin/start-nsys-ui

USER viewer
WORKDIR /home/viewer
ENV DISPLAY=:99 \
    HOME=/home/viewer \
    LIBGL_ALWAYS_SOFTWARE=1 \
    QT_OPENGL=software \
    QT_X11_NO_MITSHM=1

EXPOSE 6080
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=8 \
    CMD curl -fsS http://127.0.0.1:6080/vnc.html >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-nsys-ui"]
