FROM python:3.11-slim

WORKDIR /app

# 依存ライブラリのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 全ファイルを /app にコピー
COPY . .

EXPOSE 8501

# src/app.py ではなく app.py に指定を変更
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.enableCORS=false", "--server.headless=true"]
