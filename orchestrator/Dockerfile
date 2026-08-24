FROM registry.redhat.io/openshift4/ose-cli:latest AS oc-source

FROM python:3.12-slim

RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    git gettext-base && rm -rf /var/lib/apt/lists/*

COPY --from=oc-source /usr/bin/oc /usr/local/bin/oc

COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src

CMD ["agentd-orchestrate"]
