#!/usr/bin/env bash
set -Eeuo pipefail

echo "Restoring GridWise source payload..."
cat payload/part00 payload/part01 payload/part02 payload/part03 | base64 -d > /tmp/gridwise.tar.gz
tar -xzf /tmp/gridwise.tar.gz

echo "Installing Python dependencies..."
python -m pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

echo "Build complete."
