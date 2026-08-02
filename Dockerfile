FROM python:3.11-slim

# 作業ディレクトリを /app に指定
WORKDIR /app

# 依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# プロジェクト全体をコピー
COPY . .

# 作業ディレクトリを src に移動
WORKDIR /app/src

EXPOSE 8501

# src の中にいる状態で app.py を起動
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.enableCORS=false", "--server.headless=true"]
