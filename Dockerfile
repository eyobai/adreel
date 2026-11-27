# AdReel image: render engine + AI layer CLI. Phase 3 adds bot/worker services via compose.
FROM python:3.12-slim-bookworm

# ffmpeg: Debian's build includes libass (+HarfBuzz/FriBidi), libzimg (zscale, HDR tone
# mapping), libx264 and libmp3lame. libegl1/libgles2: required by MediaPipe's native library
# even on CPU. libgl1/libglib2.0-0: required by OpenCV (PySceneDetect).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libegl1 libgles2 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ADREEL_WORK_DIR=/data/work \
    ADREEL_LOG_JSON=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[face]"

RUN useradd --create-home --uid 1000 adreel && mkdir -p /data/work && chown -R adreel /data
USER adreel
VOLUME ["/data"]

ENTRYPOINT ["adreel"]
CMD ["--help"]
