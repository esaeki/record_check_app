FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN echo "===== BUILD FILE CHECK =====" && \
    find /app -maxdepth 3 -type f | sort

EXPOSE 8501

CMD ["sh", "-c", "streamlit run src/app.py --server.port=$PORT --server.address=0.0.0.0 --server.enableCORS=false --server.headless=true"]
