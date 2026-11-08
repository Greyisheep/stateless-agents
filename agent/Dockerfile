FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py start.sh start-shell.sh ./
ENV PYTHONUNBUFFERED=1
CMD ["/app/start.sh"]
