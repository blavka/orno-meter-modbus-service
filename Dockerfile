FROM python:3.13-alpine

WORKDIR /app
COPY pyproject.toml ./
COPY orno_meter_service ./orno_meter_service
RUN pip install --no-cache-dir .

USER 10001:10001
ENTRYPOINT ["python", "-m", "orno_meter_service.service"]
