FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS runtime
COPY requirements.lock /fixture/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /fixture/requirements.lock
COPY src /fixture/src
ENV PYTHONPATH=/fixture/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY issuer.pem probe.py endpoint.py dns_probe.py dns_trap.py dns_boundary_probe.py /fixture/
USER 65532:65532
FROM runtime AS probe
CMD ["python", "/fixture/probe.py"]
FROM runtime AS endpoint
COPY --chown=65532:65532 issuer-key.pem /fixture/issuer-key.pem
CMD ["python", "/fixture/endpoint.py"]
