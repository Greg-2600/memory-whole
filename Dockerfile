# Minimal Dockerfile to serve the generated `output/` directory on port 4747
FROM python:3.11-slim

# Set working dir
WORKDIR /app

# Copy only what's needed
COPY requirements.txt ./
# Install cron and small utilities needed to run service and cron
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r requirements.txt scikit-learn numpy || true

# Copy project files
COPY . /app

# copy cron job into the image (will persist when container runs)
COPY scripts/container_cron /etc/cron.d/memory_mountain
RUN chmod 0644 /etc/cron.d/memory_mountain \
 && mkdir -p /app/logs /app/output

# Ensure entrypoint is executable and expose port
RUN chmod +x ./scripts/docker_entrypoint.sh
ENV PORT=4747
EXPOSE 4747

# Entrypoint: backpopulate (if needed), generate digest/calendar, then serve
ENTRYPOINT ["./scripts/docker_entrypoint.sh"]

# Healthcheck: verify the static server responds on /calendar.html
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD curl -f http://localhost:4747/calendar.html || exit 1
