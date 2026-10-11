# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS application

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/recordbench/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl3 \
        openssl \
        curl \
        ffmpeg \
        imagemagick \
        libnss-sss \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/recordbench/app
RUN python -m venv /opt/recordbench/venv
COPY pyproject.toml README.md ./
COPY src ./src
COPY requirements /opt/recordbench/requirements
RUN pip install --no-cache-dir --no-build-isolation --require-hashes -r /opt/recordbench/requirements/build.lock \
    && pip install --no-cache-dir --no-build-isolation --require-hashes -r /opt/recordbench/requirements/application.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation . \
    && pip check

ENV HOME=/tmp/recordbench-home
EXPOSE 8786
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import json,urllib.request; p=json.load(urllib.request.urlopen('http://127.0.0.1:8786/health',timeout=3)); assert p['status'] in {'ok','degraded'} and p['storage']['status']=='ready'"

CMD ["exculpata-workbench", "--host", "0.0.0.0", "--port", "8786", "--runtime", "/var/lib/recordbench/runtime"]

FROM application AS retrieval
# PyTorch native GPU kernels can compile launchers even during eager execution.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --no-build-isolation --require-hashes -r /opt/recordbench/requirements/retrieval.lock \
    && pip check
EXPOSE 8787
HEALTHCHECK --interval=20s --timeout=5s --start-period=180s --retries=18 \
  CMD python -c "import json,urllib.request; p=json.load(urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3)); assert p['status']=='ok' and p['embedding_loaded'] and p['reranker_loaded']"
CMD ["exculpata-retrieval-worker", "--host", "0.0.0.0", "--port", "8787"]
