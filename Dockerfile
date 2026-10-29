# TargetFinder as a container. Default command is the MCP server over stdio; any CLI command works:
#   docker build -t targetfinder .
#   docker run -v targetfinder-data:/data targetfinder init
#   docker run -i -v targetfinder-data:/data targetfinder          # MCP server (stdio)
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY targetfinder ./targetfinder
RUN pip install --no-cache-dir .
ENV TARGETFINDER_DATA_DIR=/data TARGETFINDER_SWEEP_DIR=/data/sweeps TARGETFINDER_API_HOST=0.0.0.0
VOLUME ["/data"]
EXPOSE 8787
ENTRYPOINT ["targetfinder"]
CMD ["mcp"]
