FROM python:3.11-slim

WORKDIR /app

# 依存ライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 全ファイルを /app にコピー
COPY . .

EXPOSE 8501

# Streamlitの起動コマンド（直下・src配下の両方を検索できるようにPythonモジュール形式で起動、または直下のパス調整）
CMD ["streamlit", "run", "src/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.enableCORS=false", "--server.headless=true"]
