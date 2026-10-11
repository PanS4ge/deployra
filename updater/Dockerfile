# Deployra — aktualizator (sidecar). Budowany z katalogu updater/:
#   docker build -t deployra-updater updater/
# docker:cli = Alpine + klient Dockera z wtyczką compose; dokładamy tylko git (aktualizacje ze źródeł).
FROM docker:27-cli

RUN apk add --no-cache git
COPY deployra-updater.sh /usr/local/bin/deployra-updater.sh
RUN chmod +x /usr/local/bin/deployra-updater.sh

LABEL deployra.component=updater
ENTRYPOINT ["/usr/local/bin/deployra-updater.sh"]
CMD ["watch"]
