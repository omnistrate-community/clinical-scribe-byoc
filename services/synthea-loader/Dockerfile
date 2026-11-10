FROM eclipse-temurin:17-jre
ARG SYNTHEA_URL=https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/synthea /work \
    && curl -fsSL -o /opt/synthea/synthea.jar "$SYNTHEA_URL" \
    && chown 10001 /work
COPY load.sh /opt/synthea/load.sh
RUN chmod +x /opt/synthea/load.sh
USER 10001
WORKDIR /work
ENTRYPOINT ["/opt/synthea/load.sh"]
