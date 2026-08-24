FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    git gettext-base curl && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz" \
    | tar xz -C /usr/local/bin oc kubectl

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

CMD ["agentd-orchestrate"]
