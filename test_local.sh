#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# test_local.sh — Quick local smoke test for telegram-media-store
#
# 1. Generates test images with Python
# 2. Runs pytest (mocked Telegram API)
# 3. Tests CLI commands with a temporary DB
# ============================================================================

cd "$(dirname "$0")"

echo "📦 Installing package in dev mode..."
pip install -e ".[dev,server]" -q 2>/dev/null || pip install -e ".[dev,server]"

echo ""
echo "🧪 Running pytest..."
python -m pytest tests/ -v --tb=short
PYTEST_EXIT=$?

echo ""
echo "🖼️ Generating test images..."
TMPDIR=$(mktemp -d)
python3 -c "
from PIL import Image
import os, sys
d = sys.argv[1]
for i, color in enumerate(['red', 'green', 'blue', 'yellow', 'purple']):
    img = Image.new('RGB', (100, 100), color=color)
    img.save(os.path.join(d, f'test_{color}.jpg'), 'JPEG')
print(f'Created 5 test images in {d}')
" "$TMPDIR"

echo ""
echo "📊 Testing CLI (stats with empty DB)..."
export TG_STORE_DB="$TMPDIR/test_vault.db"
export BOT_TOKEN="${BOT_TOKEN:-fake_token_for_testing}"
export CHANNEL_ID="${CHANNEL_ID:--1001234567890}"

tg-media-store --db "$TG_STORE_DB" stats

echo ""
echo "📤 Testing CLI upload (will fail without real token — expected)..."
tg-media-store --db "$TG_STORE_DB" upload "$TMPDIR" 2>&1 || echo "  ↑ Expected failure without real BOT_TOKEN"

echo ""
echo "📊 Stats after upload attempt..."
tg-media-store --db "$TG_STORE_DB" stats

echo ""
echo "🧹 Cleanup..."
rm -rf "$TMPDIR"

echo ""
if [ $PYTEST_EXIT -eq 0 ]; then
    echo "✅ All tests passed!"
else
    echo "❌ Some tests failed (exit code: $PYTEST_EXIT)"
    exit $PYTEST_EXIT
fi
