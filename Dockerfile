# Use the official Python image as a base
FROM python:3.9-slim

WORKDIR /app

COPY . /app

RUN pip install --upgrade pip && pip install -r requirements.txt

CMD gunicorn app:app --bind 0.0.0.0:${PORT}
