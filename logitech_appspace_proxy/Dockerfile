ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM $BUILD_FROM

RUN apk add --no-cache \
    python3 \
    py3-cryptography

WORKDIR /app
COPY proxy.py /app/proxy.py
COPY appspace-script-template.txt /app/appspace-script-template.txt

EXPOSE 8080

CMD ["python3", "-u", "/app/proxy.py"]
