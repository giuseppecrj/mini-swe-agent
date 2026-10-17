FROM python:3.11-slim-bookworm

# The grader/acceptance criteria are deliberately NOT included in this image.
RUN apt-get update && apt-get install -y --no-install-recommends git bash && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE.md /tmp/mini-src/
COPY src/ /tmp/mini-src/src/
RUN pip install --no-cache-dir /tmp/mini-src && rm -rf /tmp/mini-src
ENV MSWEA_CONFIGURED=1 HOME=/tmp
WORKDIR /work
ENTRYPOINT ["mini"]
