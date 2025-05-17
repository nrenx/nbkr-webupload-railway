# Deploying on Railway.app

This document provides instructions for deploying the web scraping system on Railway.app.

## Memory Optimization for Railway

Railway.app has strict memory limits on its free tier. To ensure the web scraping system works correctly on Railway, the following optimizations have been implemented:

1. **Force Requests-Based Scraping**: The system automatically detects when running on Railway and uses requests-based scraping instead of Selenium to reduce memory usage.

2. **Chrome Memory Optimization**: When Selenium is used, Chrome is configured with memory-saving options to reduce its footprint.

3. **Environment Variables**: The `FORCE_REQUESTS_SCRAPING` environment variable is set to `true` to ensure requests-based scraping is used.

## Deployment Steps

1. Create a new project on Railway.app
2. Connect your GitHub repository
3. Deploy the application
4. Set the following environment variables:
   - `FORCE_REQUESTS_SCRAPING=true`
   - `SECRET_KEY=your_secret_key`

## Troubleshooting

If you encounter memory-related issues on Railway, try the following:

1. Ensure the `FORCE_REQUESTS_SCRAPING` environment variable is set to `true`
2. Reduce the number of concurrent workers to 1
3. Increase the timeout value to allow more time for requests to complete
4. Use the `--force-requests` flag when running scripts directly

## Monitoring

Railway provides logs and metrics for your application. Monitor these to ensure the application is running correctly and not exceeding memory limits.

## Railway Configuration

The `railway.toml` file contains the configuration for Railway.app. This file sets the environment variables, build configuration, and resource limits for the application.

```toml
# Railway.app configuration file

# Environment variables
[env]
FORCE_REQUESTS_SCRAPING = "true"
RAILWAY_MEMORY_LIMIT = "512MB"
PYTHONUNBUFFERED = "1"

# Build configuration
[build]
builder = "nixpacks"
buildCommand = "pip install -r requirements.txt"

# Deploy configuration
[deploy]
startCommand = "python app.py"
healthcheckPath = "/"
healthcheckTimeout = 300
restartPolicyType = "on-failure"
restartPolicyMaxRetries = 5

# Resource configuration
[resources]
cpu = 1
memory = 512
```
