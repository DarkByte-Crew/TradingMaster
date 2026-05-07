# This file is not needed separately because the server is launched from bot.py.
# However, Render expects a web service, so we keep it.
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "AI Trading Bot is online."

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8080)