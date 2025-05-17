# Use the Microsoft Playwright image which comes with browsers pre-installed
FROM mcr.microsoft.com/playwright/python:latest

# Set up working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt && pip install gunicorn

# Copy the rest of the application
COPY . .

# Make sure scripts are executable
RUN chmod +x *.py

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the application
CMD gunicorn app:app --bind 0.0.0.0:$PORT
