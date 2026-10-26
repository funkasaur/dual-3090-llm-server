# Wyoming bridge for Qwen3-TTS (qwentts.cpp tts-server on a GPU): no model here, just HTTP + sox tempo.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends sox && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir wyoming==1.7.2 num2words==0.5.14
COPY server.py words.json /app/
CMD ["python", "-u", "/app/server.py"]
