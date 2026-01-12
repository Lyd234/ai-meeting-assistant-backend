#!/usr/bin/env bash
# Render build script for Meeting Assistant Backend

set -e

echo "📦 Installing system dependencies..."

# Install FFmpeg and all required libraries for PyAV
apt-get update
apt-get install -y \
    ffmpeg \
    libavcodec-dev \
    libavformat-dev \
    libavdevice-dev \
    libavutil-dev \
    libavfilter-dev \
    libswscale-dev \
    libswresample-dev \
    pkg-config \
    python3-dev \
    build-essential

echo "🐍 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "✅ Build complete!"