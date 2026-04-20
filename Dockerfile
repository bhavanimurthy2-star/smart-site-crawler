# Official Playwright Python image — includes Chromium + all Linux system deps
FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Ensure output directory exists for reports
RUN mkdir -p reports/output

# Render injects PORT at runtime; default to 10000 for local Docker testing
CMD gunicorn --bind 0.0.0.0:${PORT:-10000} app:app
