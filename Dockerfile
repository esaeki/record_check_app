# Python 3.11 の軽量イメージを使用
FROM python:3.11-slim

# 作業ディレクトリの設定
WORKDIR /app

# 依存パッケージのインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# Streamlit 用のポート（8501）を開放
EXPOSE 8501

# Streamlit アプリの起動コマンド
CMD ["streamlit", "run", "src/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
