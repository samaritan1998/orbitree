FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV NODE_PATH=/usr/local/lib/node_modules

ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    fonts-liberation \
    fonts-noto-cjk \
    libreoffice-impress \
    nodejs \
    npm \
    poppler-utils \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pptxgenjs \
    && pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
        "markitdown[pptx]" pillow python-pptx

RUN useradd --create-home --uid 10001 pptuser \
    && mkdir -p /workspace /inputs /home/pptuser/.claude/skills \
    && chown -R pptuser:pptuser /workspace /home/pptuser

USER pptuser
WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/bin/bash"]
