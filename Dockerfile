FROM python:3.12-slim

# Install Node.js (required for Claude Code CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set up workspace
WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code (tests excluded from production image)
COPY src/ src/
RUN uv sync --frozen --no-dev

# Claude Code work directory (mount your project here)
RUN mkdir -p /workspace
VOLUME /workspace

# Session store persistence
VOLUME /app/data

ENV AGENT_BRIDGE_CLAUDE_WORK_DIR=/workspace \
    AGENT_BRIDGE_SESSION_STORE_PATH=/app/data/sessions.json \
    AGENT_BRIDGE_CLAUDE_PERMISSION_MODE=acceptEdits

CMD ["uv", "run", "agent-bridge"]
