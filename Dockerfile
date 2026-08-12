FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /root/.local /root/.local
COPY . .
ENV PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

# HEALTHCHECK тут навмисно немає: бот працює в режимі long polling і не
# піднімає HTTP-сервер, а в python:slim немає curl — стара перевірка
# http://localhost:8000/health падала завжди. На Koyeb це Worker Service,
# він не має порту й здоров'я перевіряє за самим процесом.
CMD ["python", "run.py"]
