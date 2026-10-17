FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Installer release capsules can originate from an owner-only checkout (umask
# 077). Make the public program readable by the fixed non-root runtime user.
COPY proxy.py /opt/egress/proxy.py
RUN chmod 0555 /opt/egress && chmod 0444 /opt/egress/proxy.py
USER 65532:65532
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "import socket; socket.create_connection(('127.0.0.1',3128),timeout=2).close()"
CMD ["python", "-I", "/opt/egress/proxy.py"]
