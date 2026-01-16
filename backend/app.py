import os
import string
import random
import requests
from flask import Flask, request, redirect, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# ================= CONFIG =================
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///urls.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

SHRINKME_API = "https://shrinkme.io/api"
SHRINKME_API_KEY = "YOUR_SHRINKME_API_KEY"

BASE_URL = "https://your-backend-domain.com"  # e.g Render URL

db = SQLAlchemy(app)

# ================= DATABASE =================
class ShortURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    short_code = db.Column(db.String(10), unique=True, nullable=False)
    shrinkme_url = db.Column(db.String(500), nullable=False)

db.create_all()

# ================= HELPERS =================
def generate_code(length=6):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# ================= ROUTES =================
@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.json
    long_url = data.get("url")

    if not long_url:
        return jsonify({"error": "Missing URL"}), 400

    # Call ShrinkMe
    res = requests.get(SHRINKME_API, params={
        "api": SHRINKME_API_KEY,
        "url": long_url
    }).json()

    if res.get("status") != "success":
        return jsonify({"error": "ShrinkMe failed"}), 500

    shrinkme_link = res["shortenedUrl"]

    code = generate_code()
    db.session.add(ShortURL(short_code=code, shrinkme_url=shrinkme_link))
    db.session.commit()

    return jsonify({
        "short_url": f"{BASE_URL}/s/{code}"
    })

@app.route("/s/<code>")
def redirect_short(code):
    link = ShortURL.query.filter_by(short_code=code).first_or_404()
    return redirect(link.shrinkme_url, code=302)

# ================= RUN =================
if __name__ == "__main__":
    app.run()