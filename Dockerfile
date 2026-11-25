FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd -g 10001 renderr && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin renderr \
    && install -d -m 0700 -o 10001 -g 10001 /app/data
COPY renderrvpn /app/renderrvpn
USER 10001:10001
ENTRYPOINT ["python", "-m", "renderrvpn"]
