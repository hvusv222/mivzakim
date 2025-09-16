from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/health')  # חשוב! זה הנתיב ש-Render מחפש
def health():
    return "OK", 200

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
