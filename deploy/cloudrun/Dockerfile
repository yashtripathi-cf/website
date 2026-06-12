# NDAP Custom Data Commons — Cloud Run deployment
# Extends the pre-built CDC services image with custom data + templates + AI chat

FROM gcr.io/datcom-ci/datacommons-services@sha256:60a43ea2eabf0cd4e957a715d97b0ab44047001c9f4e5f55b5cf37e4cb6e6ec3

# Bake in the pre-built SQLite data
COPY data/ /ndap-data/

# Overlay custom templates
COPY templates/ /workspace/server/templates/custom_dc/custom/

# Overlay custom static assets (CSS, logos)
COPY static/ /workspace/server/dist/custom_dc/custom/

# Replace nginx config to add AI proxy routing
COPY nginx.conf /workspace/nginx.conf

# Install AI proxy dependencies
RUN pip install --no-cache-dir flask-cors requests

# Install uv and pre-fetch datacommons-mcp for the MCP server
RUN pip install --no-cache-dir uv && \
    python3 -m uv tool install datacommons-mcp==1.1.4

# Copy AI proxy server + config
COPY additional_features/ /workspace/additional_features/

# Patch shared/lib/gcp.py to skip metadata server check (causes 90s timeout on Cloud Run)
RUN sed -i 's/return True/return False  # patched for Cloud Run/' /workspace/shared/lib/gcp.py && \
    cat /workspace/shared/lib/gcp.py

# Set environment variables for NDAP
ENV DC_API_KEY=${DC_API_KEY:-} \
    MAPS_API_KEY=${MAPS_API_KEY:-} \
    OUTPUT_DIR=/ndap-data \
    USE_SQLITE=true \
    USE_CLOUDSQL=false \
    FLASK_ENV=custom \
    ENABLE_MODEL=false \
    GOOGLE_CLOUD_PROJECT=ndap-demo \
    DEBUG=true \
    IS_CUSTOM_DC=true \
    PYTHONUNBUFFERED=1 \
    DC_API_ROOT=https://api.datacommons.org \
    ENV_PREFIX=Compose \
    ENABLE_ADMIN=false \
    DC_TYPE=custom \
    CUSTOM_DC_URL=http://localhost:8080

COPY run_cloudrun.sh /workspace/run_cloudrun.sh
RUN chmod +x /workspace/run_cloudrun.sh

EXPOSE 8080

CMD ["/workspace/run_cloudrun.sh"]
