FROM python:3.13-alpine

RUN apk update
RUN apk add --no-cache build-base libffi-dev openssl-dev
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN apk del --no-cache build-base musl-dev libffi-dev openssl-dev

COPY . .

EXPOSE 2750

ENTRYPOINT ["python", "main.py"]
