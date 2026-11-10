REGISTRY ?= ghcr.io/omnistrate-community
TAG ?= 0.1.0
SERVICES := fhir-mcp scribe-agent speech-to-text synthea-loader
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: images push charts lint

images:
	for s in $(SERVICES); do docker build -t clinical-scribe/$$s:$(TAG) services/$$s; done

push:
	for s in $(SERVICES); do \
	  docker buildx build --platform $(PLATFORMS) --label org.opencontainers.image.source=https://github.com/omnistrate-community/clinical-scribe-byoc -t $(REGISTRY)/clinical-scribe/$$s:$(TAG) --push services/$$s; \
	done

charts:
	helm dependency update --skip-refresh charts/clinical-scribe
	mkdir -p dist && helm package charts/clinical-scribe -d dist

lint: charts
	helm lint charts/clinical-scribe -f examples/values-claude.yaml
	helm lint charts/clinical-scribe -f examples/values-self-hosted.yaml
