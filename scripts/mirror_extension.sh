#!/bin/bash
# Manual mirror script: run anytime to sync extension/ → D:\projects\video2project_extension
# Use when you edit extension files but don't want to commit yet.

set -e
EXTENSION_DIR="$(cd "$(dirname "$0")/../extension" && pwd)"
WIN_TARGET="/mnt/d/projects/video2project_extension"

if [ ! -d "$EXTENSION_DIR" ]; then
    echo "ERROR: extension/ not found at $EXTENSION_DIR" >&2
    exit 1
fi

mkdir -p "$WIN_TARGET"
cp -r "$EXTENSION_DIR"/. "$WIN_TARGET/"
echo "Mirrored $(ls "$EXTENSION_DIR" | wc -l) files -> D:\\projects\\video2project_extension"
echo "In Chrome: chrome://extensions -> click reload button on the extension"
