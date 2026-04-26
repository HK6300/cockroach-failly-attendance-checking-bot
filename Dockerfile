# 軽量なPython 3.10イメージ
FROM python:3.10-slim

# 環境変数の設定 (Pythonのバッファリング無効化など)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 作業ディレクトリ
WORKDIR /app

# 依存関係のコピーとインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY . .

# Render Web Serviceのポートを公開 (Flask用)
EXPOSE $PORT

# Botの起動
CMD ["python", "bot.py"]