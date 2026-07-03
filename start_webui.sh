#!/bin/bash
echo "============================================"
echo "  📈 股票投研自动化 — WebUI 管理界面"
echo "============================================"
echo ""
cd "$(dirname "$0")"
source venv/Scripts/activate
python -X utf8 -m uvicorn webui.main:app --host 0.0.0.0 --port 8080
