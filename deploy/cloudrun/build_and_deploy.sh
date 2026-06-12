#!/bin/bash
# Build and deploy NDAP Custom Data Commons to Cloud Run
set -e

PROJECT_ID="ndap-demo"
REGION="asia-south1"
REPO="ndap-docker"
IMAGE="ndap-dc"
TAG="v23"
SERVICE_NAME="ndap-dc"
FULL_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/$IMAGE:$TAG"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WEBSITE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build_context"

echo "=== Assembling build context ==="
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/data" "$BUILD_DIR/templates" "$BUILD_DIR/static" "$BUILD_DIR/additional_features"

# Copy Dockerfile + nginx config
cp "$SCRIPT_DIR/Dockerfile" "$BUILD_DIR/"
cp "$SCRIPT_DIR/nginx.conf" "$BUILD_DIR/"

# Copy pre-built data (SQLite + embeddings)
echo "Copying data from /Users/yashtripathi/ndap-eidb/datacommons/ ..."
cp -r /Users/yashtripathi/ndap-eidb/datacommons/ "$BUILD_DIR/data/datacommons/"

# Copy custom templates
echo "Copying custom templates..."
cp "$WEBSITE_DIR/server/templates/custom_dc/custom/"* "$BUILD_DIR/templates/"

# Copy custom static assets
echo "Copying custom static assets..."
cp "$WEBSITE_DIR/static/custom_dc/custom/"* "$BUILD_DIR/static/"

# Copy AI proxy server + config
echo "Copying AI proxy files..."
cp "$WEBSITE_DIR/additional_features/mcp_proxy_only.py" "$BUILD_DIR/additional_features/"
cp "$WEBSITE_DIR/additional_features/config.json" "$BUILD_DIR/additional_features/"

# Copy startup script
cp "$SCRIPT_DIR/run_cloudrun.sh" "$BUILD_DIR/"

echo "=== Build context ready ==="
du -sh "$BUILD_DIR"

echo "=== Building Docker image ==="
docker build --platform linux/amd64 -t "$FULL_IMAGE" "$BUILD_DIR"

echo "=== Pushing to Artifact Registry ==="
docker push "$FULL_IMAGE"

echo "=== Deploying to Cloud Run ==="
gcloud run deploy "$SERVICE_NAME" \
  --image "$FULL_IMAGE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --port 8080 \
  --memory 4Gi \
  --cpu 2 \
  --timeout 300 \
  --min-instances 1 \
  --max-instances 2 \
  --no-cpu-throttling \
  --cpu-boost \
  --set-env-vars "FLASK_ENV=custom,ENABLE_MODEL=false,DEBUG=false"

echo "=== Cleaning up build context ==="
rm -rf "$BUILD_DIR"

echo "=== Done! ==="
gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format="value(status.url)"
