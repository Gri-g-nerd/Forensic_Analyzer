import os
import uuid
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import boto3
import firebase_admin
import json
import io
from pypdf import PdfReader
from google import genai
from firebase_admin import credentials, auth, firestore

load_dotenv()

# --- Firebase setup ---
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- AWS S3 setup ---
s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION"),
)
BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


app = Flask(__name__)
CORS(app)  # allows your login.html/upload.html (opened as a file) to call this backend

def extract_pdf_text(s3_key):
    """Downloads the PDF from S3 into memory and extracts its text."""
    obj = s3.get_object(Bucket=BUCKET_NAME, Key=s3_key)
    pdf_bytes = obj["Body"].read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text


@app.route("/api/analyze/<case_id>", methods=["POST"])
def analyze(case_id):
    try:
        decoded = verify_token()
        uid = decoded["uid"]
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    doc_ref = db.collection("cases").document(case_id)
    doc = doc_ref.get()
    if not doc.exists:
        return jsonify({"error": "case not found"}), 404

    case = doc.to_dict()
    if case["investigatorUid"] != uid:
        return jsonify({"error": "forbidden"}), 403

    report_text = extract_pdf_text(case["s3Key"])
    if not report_text.strip():
        return jsonify({"error": "could not extract text from PDF"}), 400

    prompt = f"""You are a digital forensics assistant. Read the following forensic report text and respond ONLY with valid JSON, no other text, in this exact format:
{{
  "summary": "a short paragraph summarizing the key findings",
  "timeline": ["event 1", "event 2", "event 3"],
  "riskScore": 0,
  "riskLevel": "Low, Medium, or High"
}}

Report text:
{report_text[:15000]}
"""

    

    response = gemini_client.models.generate_content(
    model="gemini-3.6-flash",
    contents=prompt,
)
    raw = response.text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({"error": "AI response was not valid JSON", "raw": raw}), 500

    doc_ref.update({
        "summary": result.get("summary"),
        "timeline": result.get("timeline"),
        "riskScore": result.get("riskScore"),
        "riskLevel": result.get("riskLevel"),
        "status": "analyzed",
    })

    return jsonify({"status": "ok", "result": result})

def verify_token():
    """Reads the Firebase ID token from the Authorization header and verifies it."""
    id_token = request.headers.get("Authorization", "").replace("Bearer ", "")
    return auth.verify_id_token(id_token)  # raises an exception if invalid


@app.route("/api/upload", methods=["POST"])
def upload():
    try:
        decoded = verify_token()
        uid = decoded["uid"]
        email = decoded.get("email", "")
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "empty filename"}), 400

    # Build a unique S3 key so files never overwrite each other
    unique_name = f"reports/{uid}/{uuid.uuid4()}_{file.filename}"

    # Upload to S3
    s3.upload_fileobj(file, BUCKET_NAME, unique_name)

    # Save case metadata in Firestore
    doc_ref = db.collection("cases").document()
    doc_ref.set({
        "investigatorUid": uid,
        "investigatorEmail": email,
        "fileName": file.filename,
        "s3Key": unique_name,
        "uploadDate": datetime.utcnow().isoformat(),
        "status": "uploaded",
        "summary": None,
        "timeline": None,
        "riskScore": None,
    })

    return jsonify({"status": "ok", "caseId": doc_ref.id})


@app.route("/api/cases", methods=["GET"])
def list_cases():
    try:
        decoded = verify_token()
        uid = decoded["uid"]
    except Exception:
        return jsonify({"error": "unauthorized"}), 401

    cases_ref = db.collection("cases").where("investigatorUid", "==", uid)
    cases = [{"id": doc.id, **doc.to_dict()} for doc in cases_ref.stream()]
    return jsonify(cases)


if __name__ == "__main__":
    app.run(debug=True, port=5000)