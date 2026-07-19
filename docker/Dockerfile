FROM python:3.12.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox \
    && mkdir --parents /workspace \
    && chown sandbox:sandbox /workspace

WORKDIR /workspace
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=3s --start-period=1s --retries=3 \
    CMD ["python", "-c", "import os,sys; sys.exit(0 if os.getuid() != 0 and os.path.isdir('/workspace') else 1)"]

CMD ["python", "-c", "import time; time.sleep(2147483647)"]
