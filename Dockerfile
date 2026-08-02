FROM python:3.11-slim

WORKDIR /app

# 依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# プロジェクト全体を明示的にコピー
COPY . /app

EXPOSE 8501

# WORKDIRが /app なので、/app/src/app.py を指すように指定
CMD ["streamlit", "run", "src/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.enableCORS=false", "--server.headless=true"]
