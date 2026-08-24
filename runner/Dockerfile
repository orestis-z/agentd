ARG DEVENV_BASE=ghcr.io/neuralmagic/devenv:latest
FROM ${DEVENV_BASE}

# Pre-clone repos so agentd pods don't need to fetch them at startup
RUN git clone --depth 1 https://github.com/vllm-project/compressed-tensors.git /repos-cache/compressed-tensors \
 && git clone --depth 1 https://github.com/vllm-project/llm-compressor.git /repos-cache/llm-compressor \
 && git clone --depth 1 https://github.com/vllm-project/speculators.git /repos-cache/speculators \
 && git clone --depth 1 https://github.com/vllm-project/vllm.git /repos-cache/vllm \
 && git clone --depth 1 https://github.com/neuralmagic/llm-compressor-testing.git /repos-cache/llm-compressor-testing

# Pre-install agentd
COPY . /tmp/agentd-src
RUN pip install --no-cache-dir /tmp/agentd-src && rm -rf /tmp/agentd-src

COPY runner/entrypoint-wrapper.sh /entrypoint-wrapper.sh
RUN chmod +x /entrypoint-wrapper.sh
ENTRYPOINT ["/entrypoint-wrapper.sh"]
