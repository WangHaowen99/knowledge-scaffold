# Same layout as DA's deploy-dev/Dockerfile (agent-solution, Apache-2.0) with
# the JRE image as a build arg: every DA module targets Java 17, so hosts that
# cannot pull eclipse-temurin:21 can use a local 17 JRE.
ARG JRE_IMAGE=eclipse-temurin:21-jre-jammy
FROM ${JRE_IMAGE} AS jdk
FROM python:3.11-slim
WORKDIR /app
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url $PIP_INDEX_URL} pandas openpyxl
COPY engine/target/deepanalyze-engine-*-exec.jar app.jar
COPY scenarios/ /app/scenarios/
RUN groupadd --system da && useradd --system --gid da --home /app da \
  && mkdir -p /app/data /app/skills && chown -R da:da /app
USER da
ENV SERVER_PORT=8192
EXPOSE 8192
HEALTHCHECK --interval=10s --timeout=5s --retries=8 --start-period=90s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8192/.well-known/agent-card.json', timeout=3)"
ENTRYPOINT ["java", "-Dlogback.configurationFile=classpath:logback-da.xml", "-jar", "/app/app.jar"]
