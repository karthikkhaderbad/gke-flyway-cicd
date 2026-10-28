# Builder is Alpine too: packages must be built against musl to run on the final Alpine image.
# The compilers are only a fallback for deps without musllinux wheels and never reach the final image.
FROM python:3.12-alpine AS builder
RUN apk add --no-cache gcc musl-dev
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN adduser -S -u 10001 appuser
COPY --from=builder /install /usr/local
WORKDIR /srv
COPY app ./app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
