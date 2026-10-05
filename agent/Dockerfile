FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip freeze > /app/versions.txt
COPY *.py ./
CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8090"]
