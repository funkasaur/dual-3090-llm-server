FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir tuya-device-sharing-sdk==0.2.15 prometheus_client qrcode[pil] cryptography paho-mqtt requests
COPY login.py exporter.py ./
EXPOSE 9106
CMD ["python", "exporter.py"]
