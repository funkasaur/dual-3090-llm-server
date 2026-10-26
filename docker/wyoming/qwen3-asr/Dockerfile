# Qwen3-ASR Wyoming server, CPU only (torch from the CPU wheel index: no CUDA libraries).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir qwen-asr wyoming==1.7.2
COPY server.py fixes.json vocab.txt /app/
ENV HF_HOME=/data/hf
CMD ["python", "-u", "/app/server.py"]
