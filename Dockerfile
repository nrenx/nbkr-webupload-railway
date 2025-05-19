# Use Python 3.9 as the base image
FROM python:3.9-slim

# Set up working directory
WORKDIR /app

# Install system dependencies for Chrome and Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libwayland-client0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    xdg-utils \
    libu2f-udev \
    libvulkan1 \
    curl \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt && pip install gunicorn

# Install Playwright and browsers with specific version to avoid compatibility issues
RUN pip install playwright==1.40.0 && playwright install chromium --with-deps

# Copy the rest of the application
COPY . .

# Make sure scripts are executable
RUN chmod +x *.py

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Add healthcheck
HEALTHCHECK --interval=30s --timeout=30s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:$PORT/ || exit 1

# Expose the port
EXPOSE $PORT

# Run the application with optimized settings for Railway
CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2 --threads 2 --worker-class gthread
