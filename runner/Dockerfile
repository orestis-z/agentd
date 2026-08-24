ARG DEVENV_BASE=ghcr.io/neuralmagic/devenv:latest
FROM ${DEVENV_BASE}

# Pre-clone repos so agentd pods don't need to fetch them at startup
RUN git clone --depth 1 https://github.com/vllm-project/compressed-tensors.git /repos-cache/compressed-tensors \
 && git clone --depth 1 https://github.com/vllm-project/llm-compressor.git /repos-cache/llm-compressor \
 && git clone --depth 1 https://github.com/vllm-project/speculators.git /repos-cache/speculators \
 && git clone --depth 1 https://github.com/vllm-project/vllm.git /repos-cache/vllm

# Pre-install agentd
COPY . /tmp/agentd-src
RUN pip install --no-cache-dir /tmp/agentd-src && rm -rf /tmp/agentd-src
