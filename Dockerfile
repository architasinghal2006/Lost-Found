FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements-clip.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Build with --build-arg WITH_CLIP=1 to include CPU-only PyTorch and open_clip (large download).
ARG WITH_CLIP=0
RUN if [ "$WITH_CLIP" = "1" ]; then \
      pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir open_clip_torch; \
    fi

COPY . .

# Database and uploaded photos live in /app/instance, which docker-compose keeps in a volume.
ENV LF_DB=/app/instance/lostfound.db \
    LF_UPLOADS=/app/instance/uploads
EXPOSE 8000

# Seed only on the first start, so a restart keeps the demo state.
CMD ["sh", "-c", "mkdir -p /app/instance && ([ -f \"$LF_DB\" ] || python seed.py) && uvicorn main:app --host 0.0.0.0 --port 8000"]
