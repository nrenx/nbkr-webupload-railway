# Use the official Playwright Python image (includes all browser dependencies)
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY . /app

RUN pip install --upgrade pip && pip install -r requirements.txt

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000"]
