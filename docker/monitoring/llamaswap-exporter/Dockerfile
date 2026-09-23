FROM python:3.12-slim
WORKDIR /app
COPY llamaswap_exporter.py .
RUN pip install --no-cache-dir requests prometheus_client
EXPOSE 9105
CMD ["python", "llamaswap_exporter.py"]
