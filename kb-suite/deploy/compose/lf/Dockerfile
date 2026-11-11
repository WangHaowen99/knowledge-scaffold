# Loop Forest 服务镜像：构建上下文是导出的 lf-service/target（只含打好的 lf-service.jar）。
ARG JRE_IMAGE=eclipse-temurin:17-jre
FROM ${JRE_IMAGE}
WORKDIR /app
COPY lf-service.jar /app/lf-service.jar
EXPOSE 8193
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=70", "-jar", "/app/lf-service.jar"]
