import os

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, make_response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from PIL import Image
from datetime import datetime
import json
import uuid
import numpy as np
import numbers

# --- Fixes for NumPy 2.x backward-compatibility (for colormath, etc.) ---
if not hasattr(np, "scalar"):
    np.scalar = np.generic
if not hasattr(np, "number"):
    np.number = numbers.Number
if not hasattr(np, "asscalar"):
    np.asscalar = lambda a: a.item() if hasattr(a, "item") else a
from sklearn.cluster import KMeans
import io
import cv2
from colormath.color_objects import LabColor
from colormath.color_diff import delta_e_cie2000
import json
import uuid
import hashlib
from datetime import datetime
import csv
import logging
import google.auth

import base64
import random
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OTP_SALT, OTP_EXPIRY_MINUTES, USER_CSV, RESET_CSV

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config['SECRET_KEY'] = 'supersecretkey'

# Initialize Flask-Login
login_manager = LoginManager()

class User(UserMixin):
    def __init__(self, id):
        self.id = id

# Replace with your actual user data (will be overwritten by load from CSV if present)
users = {'admin': {'password': 'password'}}
user_objects = {'admin': User(id='admin')}

def load_users_from_csv():
    """
    Load users from USER_CSV into the in-memory `users` dict.
    Expected CSV header: username,password_hash
    """
    global users
    if not os.path.exists(USER_CSV):
        return
    users = {}
    with open(USER_CSV, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # keep key names defensive
            uname = row.get('username')
            ph = row.get('password_hash') or row.get('password') or ''
            users[uname] = {'password_hash': ph}

@login_manager.user_loader
def load_user(user_id):
    return user_objects.get(user_id)

login_manager.init_app(app)

# Load users from CSV into memory on startup (if file exists)
try:
    load_users_from_csv()
except Exception:
    # keep default users dict if anything fails
    pass

# --- Google Sheets setup ---
spreadsheet = None
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    from config import SPREADSHEET_ID

    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if service_account_json and SPREADSHEET_ID:
        creds_dict = json.loads(service_account_json)

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]

        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=scopes
        )

        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        logger.info("✅ Google Sheets connected successfully (Render).")
    else:
        logger.warning("⚠️ Google Sheets disabled (missing env vars).")

except Exception as e:
    logger.exception("⚠️ Failed to connect to Google Sheets: %s", e)

def append_reference_to_sheet(ref_id, label, doctor, color_features, image_hash):
    if not spreadsheet: return
    try:
        try:
            ws = spreadsheet.worksheet('reference_colors')
        except:
            ws = spreadsheet.add_worksheet(title='reference_colors', rows=1000, cols=20)
        hexv = color_features.get('hex')
        rgb = color_features.get('rgb', [None, None, None])
        hsv = color_features.get('hsv', [None, None, None])
        lab = color_features.get('lab', [None, None, None])
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        row = [
            ref_id, label, doctor, hexv,
            rgb[0], rgb[1], rgb[2],
            hsv[0], hsv[1], hsv[2],
            lab[0], lab[1], lab[2],
            image_hash, created_at
        ]
        ws.append_row(row, value_input_option='USER_ENTERED')
    except Exception as e:
        logger.exception("Failed to append reference: %s", e)

def append_analysis_to_sheet(analysis_id, patient_info, analysis_date, extracted_color_features, matched_label, matched_hex, match_score, matched_by_doctor):
    if not spreadsheet: return
    try:
        try:
            ws = spreadsheet.worksheet('analysis_history')
        except:
            ws = spreadsheet.add_worksheet(title='analysis_history', rows=1000, cols=20)
        row = [
            analysis_id,
            patient_info.get('name'),
            patient_info.get('age'),
            patient_info.get('sex'),
            analysis_date,
            json.dumps(extracted_color_features),
            matched_label,
            matched_hex,
            match_score,
            matched_by_doctor
        ]
        ws.append_row(row, value_input_option='USER_ENTERED')
    except Exception as e:
        logger.exception("Failed to append analysis: %s", e)

# ----------------- Helpers -----------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_color_features(image):
    image_resized = image.resize((100, 100))
    np_image = np.array(image_resized.convert('RGB'))
    np_image = cv2.GaussianBlur(np_image, (5, 5), 0)  # Noise reduction
    pixels = np_image.reshape(-1, 3)
    kmeans = KMeans(n_clusters=1, n_init=10, random_state=0)
    kmeans.fit(pixels)
    dominant_rgb = np.round(kmeans.cluster_centers_[0]).astype(int)
    r, g, b = dominant_rgb
    hex_color = '#{:02x}{:02x}{:02x}'.format(r, g, b)
    rgb_for_cv = np.uint8([[[r, g, b]]])
    hsv_color = cv2.cvtColor(rgb_for_cv, cv2.COLOR_RGB2HSV)[0][0]
    lab_color = cv2.cvtColor(rgb_for_cv, cv2.COLOR_RGB2LAB)[0][0]
    return {'rgb': [int(r), int(g), int(b)], 'hex': hex_color, 'hsv': [int(hsv_color[0]), int(hsv_color[1]), int(hsv_color[2])], 'lab': [int(lab_color[0]), int(lab_color[1]), int(lab_color[2])]}

def hash_value(value):
    return hashlib.sha256((value + OTP_SALT).encode()).hexdigest()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload)
    except:
        pass

def generate_otp():
    return str(random.randint(100000, 999999))

def save_otp(username, otp):
    import csv
    from datetime import datetime, timedelta
    expires_at = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    otp_hash = hash_value(otp)
    # ensure header if file doesn't exist
    file_exists = os.path.exists(RESET_CSV)
    with open(RESET_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["username","otp_hash","expires_at","used"])
        writer.writerow([username, otp_hash, expires_at, "False"])

def verify_otp(username, otp):
    import csv
    from datetime import datetime
    if not os.path.exists(RESET_CSV):
        return False
    valid = False
    rows = []
    with open(RESET_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # defensive checks in case fields missing
            if (
                row.get("username") == username
                and row.get("otp_hash") == hash_value(otp)
                and row.get("used", "False") == "False"
                and datetime.now() < datetime.strptime(row.get("expires_at"), "%Y-%m-%d %H:%M:%S")
            ):
                row["used"] = "True"
                valid = True
            rows.append(row)
    # write back updated rows (preserve header)
    with open(RESET_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["username", "otp_hash", "expires_at", "used"])
        writer.writeheader()
        writer.writerows(rows)
    return valid

def update_password(username, new_password):
    import csv
    new_hash = hash_value(new_password)
    rows = []
    # If users file doesn't exist, create it and add header
    if not os.path.exists(USER_CSV):
        with open(USER_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["username", "password_hash"])
            writer.writeheader()
    with open(USER_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)
        found = False
        for row in reader:
            if row.get("username") == username:
                row["password_hash"] = new_hash
                found = True
            rows.append(row)
    # if user not found, append it
    if not any(r.get("username") == username for r in rows):
        rows.append({"username": username, "password_hash": new_hash})
    with open(USER_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["username", "password_hash"])
        writer.writeheader()
        writer.writerows(rows)

# ----------------- Routes -----------------

@app.route('/login', methods=['GET','POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('doctor_page'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        # check user exists in memory
        if username in users:
            # try stored hash / fallback to plain password
            stored_hash = users[username].get('password_hash') or users[username].get('password') or ''
            # accept if stored value equals hashed input OR equals plain password (backwards compat)
            if stored_hash == hash_value(password) or stored_hash == password:
                # ensure user object exists
                if username not in user_objects:
                    user_objects[username] = User(id=username)
                login_user(user_objects[username])
                return redirect(url_for('doctor_page'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        username = request.form.get("username")
        otp = generate_otp()
        save_otp(username, otp)
        send_telegram_message(f"🔐 OTP for {username}: {otp}")
        flash("OTP sent via Telegram!", "info")
        return redirect(url_for("verify_otp_page", username=username))
    return render_template("forgot_password.html")


@app.route("/verify-otp/<username>", methods=["GET", "POST"])
def verify_otp_page(username):
    if request.method == "POST":
        otp = request.form.get("otp")
        if verify_otp(username, otp):
            flash("OTP verified. Set new password.", "success")
            return redirect(url_for("reset_password", username=username))
        else:
            flash("Invalid or expired OTP.", "danger")
    return render_template("verify_otp.html", username=username)


@app.route("/reset-password/<username>", methods=["GET", "POST"])
def reset_password(username):
    if request.method == "POST":
        new_password = request.form.get("new_password")
        confirm_password = request.form.get("confirm_password")
        if new_password != confirm_password:
            flash("Passwords do not match!", "danger")
        else:
            update_password(username, new_password)
            # Refresh in-memory users immediately after updating CSV
            try:
                load_users_from_csv()
            except Exception:
                pass
            flash("Password updated successfully!", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html", username=username)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/')
def user_page():
    return render_template('user.html')

@app.route('/user-upload', methods=['POST'])
def user_upload():
    file = request.files.get('image')
    if not file or file.filename == '': return "No image selected", 400
    if not allowed_file(file.filename): return "Invalid file type.", 400
    patient_info = {'name': request.form.get('patient_name'), 'age': request.form.get('age'), 'sex': request.form.get('sex')}
   
    # Read image into memory and encode as Base64
    in_memory_file = io.BytesIO()
    file.save(in_memory_file)
    in_memory_file.seek(0)
    image_data = base64.b64encode(in_memory_file.read()).decode('utf-8')

    return render_template('crop_user.html', image_data=image_data, patient_info=patient_info)

@app.route('/analyze', methods=['POST'])
def analyze_stool():
    image_data_str = request.form.get('image_data')
    patient_info = {
        'name': request.form.get('patient_name'),
        'age': request.form.get('age'),
        'sex': request.form.get('sex')
    }
    if not image_data_str:
        return "Missing form data", 400

    # Decode the Base64 data URL
    image_data = base64.b64decode(image_data_str.split(',')[1])
    cropped_image = Image.open(io.BytesIO(image_data))

    # Extract features from uploaded image
    user_color_features = get_color_features(cropped_image)
    user_lab_color = np.array(user_color_features['lab'], dtype=float)

    # --- Fetch reference colors from Google Sheets ---
    try:
        ws = spreadsheet.worksheet('reference_colors')
        rows = ws.get_all_records()
    except Exception as e:
        print("⚠️ Failed to read reference colors:", e)
        rows = []

    if not rows:
        return "No reference colors in database.", 400

    best_match = None
    smallest_difference = float('inf')

    for row in rows:
        try:
            ref_lab_color = np.array(
                [float(row['lab_l']), float(row['lab_a']), float(row['lab_b'])],
                dtype=float
            )
            color1 = LabColor(
                lab_l=float(user_lab_color[0]),
                lab_a=float(user_lab_color[1]),
                lab_b=float(user_lab_color[2])
            )
            color2 = LabColor(
                lab_l=float(ref_lab_color[0]),
                lab_a=float(ref_lab_color[1]),
                lab_b=float(ref_lab_color[2])
            )

            difference = float(delta_e_cie2000(color1, color2))

            if difference < smallest_difference:
                smallest_difference = difference
                match_percentage = max(0, 100 - (smallest_difference * 2.5))
                best_match = {
                    'label': row['label'],
                    'doctor': row['doctor'],
                    'hex': row['hex'],
                    'match_score': round(smallest_difference, 2),
                    'match_percentage': int(match_percentage)
                }

        except Exception as e:
            print(f"⚠️ Skipping row due to error: {e}")
            continue

    # Save analysis if a valid match found
    analysis_id = str(uuid.uuid4())
    if best_match:
        append_analysis_to_sheet(
            analysis_id,
            patient_info,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_color_features,
            best_match['label'],
            best_match['hex'],
            best_match['match_score'],
            best_match['doctor']
        )

    return render_template(
        'result.html',
        result=best_match,
        patient_info=patient_info,
        analysis_id=analysis_id
    )

@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    try:
        print("API HIT")

        image_data_str = request.form.get('image_data')
        if not image_data_str:
            return jsonify({'error': 'Missing image'}), 400

        patient_name = request.form.get('patient_name', '').strip()
        patient_age = request.form.get('age', '').strip()
        patient_sex = request.form.get('sex', '').strip()

        print("Patient:", patient_name, patient_age, patient_sex)

        image_data = base64.b64decode(image_data_str.split(',')[1])
        cropped_image = Image.open(io.BytesIO(image_data)).convert('RGB')

        user_color_features = get_color_features(cropped_image)
        user_lab = user_color_features['lab']

        print("User LAB:", user_lab)

        ref_ws = spreadsheet.worksheet('reference_colors')
        ref_rows = ref_ws.get_all_records()

        best_match = None
        smallest = float('inf')

        for row in ref_rows:
            ref_lab = [
                float(row['lab_l']),
                float(row['lab_a']),
                float(row['lab_b'])
            ]

            d = float(delta_e_cie2000(
                LabColor(
                    lab_l=float(user_lab[0]),
                    lab_a=float(user_lab[1]),
                    lab_b=float(user_lab[2]),
                ),
                LabColor(
                    lab_l=float(ref_lab[0]),
                    lab_a=float(ref_lab[1]),
                    lab_b=float(ref_lab[2]),
                )
            ))

            if d < smallest:
                smallest = d

                match_score = round(d, 2)              # ✅ FORCE NUMBER
                match_percentage = int(
                    max(0, 100 - (float(match_score) * 2.5))
                )                                       # ✅ FIXED BUG

                best_match = {
                    'label': row['label'],
                    'hex': row['hex'],
                    'doctor': row['doctor'],
                    'match_score': match_score,
                    'match_percentage': match_percentage
                }

        print("Best Match:", best_match)

        history_ws = spreadsheet.worksheet('analysis_history')

        history_ws.append_row(
            [
                str(uuid.uuid4()),                                  # analysis_id
                patient_name,                                       # patient_name
                patient_age,                                        # patient_age
                patient_sex,                                        # patient_sex
                datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),    # analysis_date
                json.dumps(user_color_features),                    # extracted_color_features_json
                best_match['label'],                                # matched_label
                best_match['hex'],                                  # matched_hex
                best_match['match_score'],                          # match_score (NUMBER)
                best_match['doctor'],                               # matched_by_doctor
            ],
            value_input_option='RAW'
        )

        print("History row appended")

        return jsonify({
            'patient': {
                'name': patient_name,
                'age': patient_age,
                'sex': patient_sex
            },
            'result': best_match
        })

    except Exception as e:
        print("API ERROR:", repr(e))
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/api/history', methods=['GET'])
def api_history():
    try:
        ws = spreadsheet.worksheet('analysis_history')
        rows = ws.get_all_records()
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    history = []
    for r in rows:
        history.append({
            'analysis_id': r.get('analysis_id'),
            'date': r.get('analysis_date'),
            'patient_name': r.get('patient_name'),
            'age': r.get('patient_age'),
            'sex': r.get('patient_sex'),
            'label': r.get('matched_label'),
            'hex': r.get('matched_hex'),
            'match_score': r.get('match_score'),
            'match_percentage': int(max(0, 100 - (float(r.get('match_score', 0)) * 2.5)))
        })

    return jsonify(history)


@app.route('/doctor')
@login_required
def doctor_page():
    return render_template('doctor.html')

@app.route('/doctor-upload', methods=['POST'])
@login_required
def doctor_upload():
    file = request.files.get('image')
    if not file or file.filename == '':
        flash("No image selected", "warning")
        return redirect(url_for('doctor_page'))
    if not allowed_file(file.filename):
        flash("Invalid file type", "danger")
        return redirect(url_for('doctor_page'))

    # Read image into memory and encode as Base64
    in_memory_file = io.BytesIO()
    file.save(in_memory_file)
    in_memory_file.seek(0)
    image_data = base64.b64encode(in_memory_file.read()).decode('utf-8')

    return render_template('crop_doctor.html', image_data=image_data)

@app.route('/save-reference', methods=['POST'])
@login_required
def save_reference():
    try:
        image_data_str = request.form.get('image_data')
        label = request.form.get('label')
        doctor_name = request.form.get('doctor_name')
        if not all([image_data_str, label, doctor_name]): return jsonify({'status':'error','message':'Missing form data'}), 400

        # Decode the Base64 data URL
        image_data = base64.b64decode(image_data_str.split(',')[1])
        cropped_image = Image.open(io.BytesIO(image_data))

        image_bytes = io.BytesIO()
        cropped_image.save(image_bytes, format='PNG')
        image_hash = hashlib.sha256(image_bytes.getvalue()).hexdigest()

        # --- Fetch existing references to check duplicate ---
        try:
            ws = spreadsheet.worksheet('reference_colors')
            rows = ws.get_all_records()
            for row in rows:
                if row['image_hash'] == image_hash:
                    return jsonify({'status':'warning','message':'Duplicate image detected.'}), 200
        except:
            rows = []

        color_features = get_color_features(cropped_image)
        ref_id = str(uuid.uuid4())
        append_reference_to_sheet(ref_id, label, doctor_name, color_features, image_hash)
        return jsonify({'status':'success','message':f"Reference color '{label}' saved!"}), 200
    except Exception as e:
        print(f"Error: {e}")
        return jsonify({'status':'error','message':'Unexpected server error.'}), 500


@app.route('/manage')
@login_required
def manage_references():
    try:
        ws = spreadsheet.worksheet('reference_colors')
        rows = ws.get_all_records()
    except:
        rows = []
    references = [{'id': row['id'], 'label': row['label'], 'doctor': row['doctor'], 'hex': row['hex']} for row in rows]
    return render_template('manage_references.html', references=references)

@app.route('/delete/<string:ref_id>', methods=['POST'])
@login_required
def delete_reference(ref_id):
    try:
        ws = spreadsheet.worksheet('reference_colors')
        all_rows = ws.get_all_records()
        for i, row in enumerate(all_rows, start=2):
            if str(row['id']) == str(ref_id):
                ws.delete_rows(i)
                break
        flash("Reference successfully deleted!", "success")
    except Exception as e:
        flash("Delete failed: "+str(e), "danger")
    return redirect(url_for('manage_references'))

@app.route('/history')
@login_required
def history():
    search_query = request.args.get('search_name', '')

    try:
        ws = spreadsheet.worksheet('analysis_history')
        rows = ws.get_all_records()
    except Exception as e:
        print("History load error:", e)
        rows = []

    history_records = []

    for record in rows:
        if search_query.lower() in str(record.get('patient_name', '')).lower():

            raw_score = record.get('match_score', 0)

            try:
                match_score = float(raw_score)
            except (ValueError, TypeError):
                match_score = 0.0

            match_percentage = max(0, 100 - (match_score * 2.5))
            record['match_percentage'] = int(match_percentage)

            history_records.append(record)

    history_records = sorted(
        history_records,
        key=lambda x: x.get('analysis_date', ''),
        reverse=True
    )

    return render_template(
        'history.html',
        history=history_records,
        search_query=search_query
    )

@app.route('/delete-history/<string:analysis_id>', methods=['POST'])
@login_required
def delete_history(analysis_id):
    try:
        ws = spreadsheet.worksheet('analysis_history')
        all_rows = ws.get_all_records()
        for i, row in enumerate(all_rows, start=2):
            if str(row['analysis_id']) == str(analysis_id):
                ws.delete_rows(i)
                break
        flash("Patient history record successfully deleted!", "success")
    except Exception as e:
        flash("Delete failed: "+str(e), "danger")
    return redirect(url_for('history'))

@app.route('/export-csv')
@login_required
def export_csv():
    try:
        ws = spreadsheet.worksheet('analysis_history')
        rows = ws.get_all_records()
    except:
        rows = []
    si = io.StringIO()
    cw = csv.writer(si)
    if rows:
        cw.writerow(rows[0].keys())
        cw.writerows([[r for r in row.values()] for row in rows])
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=analysis_history.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/export-references-csv')
@login_required
def export_references_csv():
    try:
        ws = spreadsheet.worksheet('reference_colors')
        rows = ws.get_all_records()
    except:
        rows = []
    si = io.StringIO()
    cw = csv.writer(si)
    headers = ['id', 'label', 'doctor', 'hex', 'rgb_r','rgb_g','rgb_b','hsv_h','hsv_s','hsv_v','lab_l','lab_a','lab_b']
    cw.writerow(headers)
    for row in rows:
        row_data = [
            row['id'], row['label'], row['doctor'], row['hex'],
            row['rgb_r'], row['rgb_g'], row['rgb_b'],
            row['hsv_h'], row['hsv_s'], row['hsv_v'],
            row['lab_l'], row['lab_a'], row['lab_b']
        ]
        cw.writerow(row_data)
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=reference_colors.csv"
    output.headers["Content-type"] = "text/csv"
    return output



@app.route('/sw.js')
def service_worker():
    return app.send_static_file('sw.js')
