FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 STATE_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY signup_bot ./signup_bot
CMD ["python", "-m", "signup_bot"]
