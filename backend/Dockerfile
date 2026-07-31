FROM python:3.11-slim

ENV TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# ffmpeg is the entire native dependency list: it joins the blocks, runs the
# two-pass loudnorm, encodes the mp3 and (as ffprobe) measures every block for
# the duration sanity check. Without it the server can validate and cost a
# script but cannot produce an episode, so it is verified at build time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && ffmpeg -version > /dev/null \
 && ffprobe -version > /dev/null \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8021
EXPOSE 8021

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c 'import os,urllib.request;urllib.request.urlopen("http://127.0.0.1:%s/health" % os.environ.get("PORT","8021"), timeout=4).read()' || exit 1

# Entry through main() (not the uvicorn CLI) so PORT/LOG_LEVEL and the
# proxy_headers/keep-alive settings the reverse-proxy deployment needs apply.
CMD ["python", "main.py"]
