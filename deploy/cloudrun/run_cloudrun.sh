#!/bin/bash
# Cloud Run wrapper — starts all services including AI chat layer
set -e

export MIXER_API_KEY=$DC_API_KEY
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=1
export PYTHONUNBUFFERED=1

echo "OUTPUT_DIR=$OUTPUT_DIR"
echo "FLASK_ENV=$FLASK_ENV"

export IS_CUSTOM_DC=true
export USER_DATA_PATH=$OUTPUT_DIR
export ADDITIONAL_CATALOG_PATH=$USER_DATA_PATH/datacommons/nl/embeddings/custom_catalog.yaml

if [[ $USE_SQLITE == "true" ]]; then
    export SQLITE_PATH=$OUTPUT_DIR/datacommons/datacommons.db
    echo "SQLITE_PATH=$SQLITE_PATH"
fi

# Check SQLite file exists
if [[ -f "$SQLITE_PATH" ]]; then
    echo "SQLite file found: $(ls -la $SQLITE_PATH)"
else
    echo "WARNING: SQLite file NOT found at $SQLITE_PATH"
fi

# Start nginx (with AI proxy routing)
nginx -c /workspace/nginx.conf

# Start mixer
/workspace/bin/mixer \
    --use_bigquery=false \
    --use_base_bigtable=false \
    --use_custom_bigtable=false \
    --use_branch_bigtable=false \
    --sqlite_path=$SQLITE_PATH \
    --use_sqlite=$USE_SQLITE \
    --use_cloudsql=$USE_CLOUDSQL \
    --cloudsql_instance=$CLOUDSQL_INSTANCE \
    --remote_mixer_domain=$DC_API_ROOT &
MIXER_PID=$!
echo "Mixer started (PID $MIXER_PID)"

# Start envoy
envoy -l warning --config-path /workspace/esp/envoy-config.yaml &
echo "Envoy started (PID $!)"

# Start NL server (optional)
if [[ $ENABLE_MODEL == "true" ]]; then
    echo "Starting NL Server..."
    python3 -u nl_app.py 6060 > /tmp/nl_app.log 2>&1 &
    echo "NL Server started (PID $!)"
fi

# Start Website Server
echo "Starting Website Server on port 7070..."
python3 -u web_app.py 7070 > /tmp/web_app.log 2>&1 &
WEB_PID=$!
echo "Website Server PID=$WEB_PID"

# Start MCP Server (Data Commons knowledge graph tools)
echo "Starting MCP Server on port 3000..."
export DC_TYPE=custom
export CUSTOM_DC_URL=http://localhost:8080
python3 -m uv tool run --from datacommons-mcp==1.1.4 datacommons-mcp serve http --port 3000 --host 127.0.0.1 > /tmp/mcp_server.log 2>&1 &
MCP_PID=$!
echo "MCP Server started (PID $MCP_PID)"

# Start AI Proxy Server
echo "Starting AI Proxy on port 5001..."
cd /workspace/additional_features
python3 -u mcp_proxy_only.py > /tmp/proxy.log 2>&1 &
PROXY_PID=$!
cd /workspace
echo "AI Proxy started (PID $PROXY_PID)"

# Monitor startup — wait for web server
for i in $(seq 1 30); do
    sleep 5
    if ! kill -0 $WEB_PID 2>/dev/null; then
        echo "ERROR: Web server died after ${i}x5s!"
        cat /tmp/web_app.log 2>/dev/null || echo "(no log)"
        break
    fi
    if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', 7070)); s.close(); print('Port 7070 is UP')" 2>/dev/null; then
        echo "Web server is ready after ${i}x5s"
        break
    fi
    echo "Waiting... ${i}x5s (PID $WEB_PID alive)"
    tail -3 /tmp/web_app.log 2>/dev/null || true
done

# Show logs
echo "=== web_app.log ===" && cat /tmp/web_app.log 2>/dev/null || echo "(empty)"
echo "=== end web_app.log ==="
echo "=== mcp_server.log ===" && cat /tmp/mcp_server.log 2>/dev/null || echo "(empty)"
echo "=== end mcp_server.log ==="
echo "=== proxy.log ===" && cat /tmp/proxy.log 2>/dev/null || echo "(empty)"
echo "=== end proxy.log ==="

# Check proxy health
if python3 -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 5001)); s.close(); print('Proxy port 5001 is UP')" 2>/dev/null; then
    echo "AI Proxy is ready"
else
    echo "WARNING: AI Proxy not yet ready on port 5001"
fi

# Keep container alive — only exit if the CRITICAL web server or mixer dies
# MCP server and proxy can restart independently
while true; do
    sleep 10
    if ! kill -0 $WEB_PID 2>/dev/null; then
        echo "FATAL: Web server (PID $WEB_PID) died!"
        cat /tmp/web_app.log 2>/dev/null || true
        exit 1
    fi
    if ! kill -0 $MIXER_PID 2>/dev/null; then
        echo "FATAL: Mixer (PID $MIXER_PID) died!"
        exit 1
    fi
    # Restart proxy if it died
    if ! kill -0 $PROXY_PID 2>/dev/null; then
        echo "WARNING: Proxy died, restarting..."
        cd /workspace/additional_features
        python3 -u mcp_proxy_only.py > /tmp/proxy.log 2>&1 &
        PROXY_PID=$!
        cd /workspace
        echo "Proxy restarted (PID $PROXY_PID)"
    fi
    # Restart MCP server if it died
    if ! kill -0 $MCP_PID 2>/dev/null; then
        echo "WARNING: MCP server died, restarting..."
        python3 -m uv tool run --from datacommons-mcp==1.1.4 datacommons-mcp serve http --port 3000 --host 127.0.0.1 > /tmp/mcp_server.log 2>&1 &
        MCP_PID=$!
        echo "MCP server restarted (PID $MCP_PID)"
    fi
done
