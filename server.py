import os
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    # 死活監視用エンドポイント
    return "Bot is running perfectly!", 200

def run_server():
    # Renderは環境変数 PORT でポートを指定する
    port = int(os.environ.get("PORT", 8080))
    # 外部からのアクセスを許可するために 0.0.0.0 で起動
    app.run(host='0.0.0.0', port=port, use_reloader=False)

def start_web_server():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()