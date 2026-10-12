FROM python:3.12-slim
WORKDIR /app
COPY acs ./acs
COPY web ./web
ENV ACS_DEMO_DELAY=0.7
EXPOSE 8780
USER 10001
CMD ["python3", "-m", "acs.server", "--host", "0.0.0.0", "--port", "8780", "--data", "/tmp/tasks.json"]
