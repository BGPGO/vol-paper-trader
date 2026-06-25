FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/paper.db PORT=5000 POLL_SECONDS=300 MPLCONFIGDIR=/tmp/mpl DATA_EPOCH=3

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
RUN mkdir -p /data
VOLUME /data
EXPOSE 5000

# single worker so the background poller + sqlite state live in one process
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
