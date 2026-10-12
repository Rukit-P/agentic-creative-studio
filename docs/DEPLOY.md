# Deployment

The demo mode needs no secrets and no outbound network, so it is the safe thing to deploy publicly.

## Container

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY acs ./acs
COPY web ./web
ENV ACS_DEMO_DELAY=0.7
EXPOSE 8780
USER 10001
CMD ["python3", "-m", "acs.server", "--host", "0.0.0.0", "--port", "8780", "--data", "/tmp/tasks.json"]
```

The same file is checked in as `Dockerfile`. It was built with Docker 28.5.2 (image about 120 MB), started as UID 10001, and a `flaky` task completed through the container's API. That is the extent of the verification: no registry push and no public host were tested.

## Hosting notes

- Run demo mode only on a public host. Do not set `ACS_LLM_API_KEY` there (no authentication, no rate limiting; see SECURITY.md).
- `--host 0.0.0.0` is required inside a container; put TLS and, ideally, rate limiting in front.
- State is ephemeral in the container (`/tmp`), which suits a stateless demo.
- Any platform that runs a container or a Python process works; no platform-specific configuration is included.
