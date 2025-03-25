from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import os
import qrcode
import uuid
import base64
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from io import BytesIO
from docx import Document
from datetime import datetime  # لإضافة تاريخ الرفع

app = Flask(__name__)
app.secret_key = 'your_secret_key'  # يُفضل تغيير هذا إلى قيمة آمنة وفريدة
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
db = SQLAlchemy(app)

UPLOAD_FOLDER = 'uploads'
QR_FOLDER = 'static/qr_codes'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

# نموذج المستخدم مع إضافة علاقة مع الملفات
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True)
    password = db.Column(db.String(150))
    is_admin = db.Column(db.Boolean, default=False)
    files = db.relationship('PdfFile', backref='user', lazy=True)

# نموذج الملف مع إضافة حقل user_id و file_type و upload_date
class PdfFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_filename = db.Column(db.String(150))
    stored_filename = db.Column(db.String(200), unique=True)
    encryption_key = db.Column(db.String(500))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    file_type = db.Column(db.String(50), nullable=False)  # نوع الملف: PDF، Word، Image
    upload_date = db.Column(db.DateTime, default=datetime.utcnow)  # تاريخ الرفع

with app.app_context():
    db.create_all()

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            session['username'] = user.username
            session['is_admin'] = user.is_admin

            next_url = session.pop('next_url', None)
            return redirect(next_url or url_for('dashboard'))

        return render_template('login.html', message="خطأ في البيانات")
    return render_template('login.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'username' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        file = request.files['pdf']
        original_filename = file.filename

        # التحقق من نوع الملف
        allowed_extensions = {'.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.gif'}
        file_extension = os.path.splitext(original_filename)[1].lower()
        if file_extension not in allowed_extensions:
            return render_template('upload.html', error="نوع الملف غير مدعوم. الرجاء رفع ملف PDF، Word، أو صورة.")

        # تحديد نوع الملف
        if file_extension in {'.pdf'}:
            file_type = 'PDF'
        elif file_extension in {'.doc', '.docx'}:
            file_type = 'Word'
        else:
            file_type = 'Image'

        stored_filename = f"{uuid.uuid4().hex}_{original_filename}"
        filepath = os.path.join(UPLOAD_FOLDER, stored_filename)

        file.save(filepath)

        key = Fernet.generate_key()
        cipher = Fernet(key)

        with open(filepath, 'rb') as f:
            encrypted_data = cipher.encrypt(f.read())

        encrypted_filepath = filepath + '.enc'
        with open(encrypted_filepath, 'wb') as f:
            f.write(encrypted_data)

        os.remove(filepath)

        # الحصول على المستخدم الحالي
        current_user = User.query.filter_by(username=session['username']).first()

        # إضافة الملف مع user_id و file_type و upload_date
        new_file = PdfFile(
            original_filename=original_filename,
            stored_filename=stored_filename,
            encryption_key=key.decode(),
            user_id=current_user.id,
            file_type=file_type,
            upload_date=datetime.utcnow()
        )
        db.session.add(new_file)
        db.session.commit()

        file_url = url_for('view_file', filename=stored_filename, _external=True)
        qr = qrcode.make(file_url)
        qr_path = os.path.join(QR_FOLDER, f'{stored_filename}_qr.png')
        qr.save(qr_path)

        return render_template('upload.html', qr_code=f'/static/qr_codes/{stored_filename}_qr.png', file_url=file_url)

    return render_template('upload.html')

@app.route('/file/<filename>')
def view_file(filename):
    file_record = PdfFile.query.filter_by(stored_filename=filename).first()
    if not file_record:
        return "الملف غير موجود", 404

    if 'username' not in session:
        session['next_url'] = request.url
        return redirect(url_for('login'))

    # الحصول على المستخدم الحالي
    current_user = User.query.filter_by(username=session['username']).first()
    # التحقق مما إذا كان المستخدم هو المالك أو مسؤول
    is_owner = (current_user.id == file_record.user_id) or session.get('is_admin')

    # إذا كان المستخدم هو المالك أو مسؤول ونوع الملف Word، استخرج النص
    word_content = None
    if (is_owner or session.get('is_admin')) and file_record.file_type == 'Word':
        # التحقق من امتداد الملف
        file_extension = os.path.splitext(file_record.original_filename)[1].lower()
        if file_extension != '.docx':
            word_content = "عذرًا، عرض النص متاح فقط لملفات .docx. يمكنك تحميل الملف لفتحه."
        else:
            # فك تشفير الملف مؤقتًا
            encrypted_filepath = os.path.join(UPLOAD_FOLDER, filename + '.enc')
            cipher = Fernet(file_record.encryption_key.encode())
            try:
                with open(encrypted_filepath, 'rb') as f:
                    decrypted_data = cipher.decrypt(f.read())
            except Exception as e:
                word_content = f"خطأ أثناء فك تشفير الملف: {str(e)}"
                return render_template('view.html', filename=filename, is_admin=session.get('is_admin'), is_owner=is_owner, file_type=file_record.file_type, word_content=word_content)

            # التحقق من أن الملف ليس فارغًا
            if len(decrypted_data) == 0:
                word_content = "الملف فارغ أو تالف."
                return render_template('view.html', filename=filename, is_admin=session.get('is_admin'), is_owner=is_owner, file_type=file_record.file_type, word_content=word_content)

            # استخدام اسم مؤقت بدون مسافات أو أحرف خاصة
            temp_filename = f"temp_{uuid.uuid4().hex}.docx"
            decrypted_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)
            with open(decrypted_filepath, 'wb') as f:
                f.write(decrypted_data)

            # استخراج النص من ملف Word
            try:
                doc = Document(decrypted_filepath)
                word_content = "\n".join([para.text for para in doc.paragraphs if para.text.strip() != ""])
                if not word_content:
                    word_content = "الملف لا يحتوي على نص قابل للاستخراج."
            except Exception as e:
                word_content = f"خطأ أثناء استخراج النص: {str(e)}"
            finally:
                if os.path.exists(decrypted_filepath):
                    os.remove(decrypted_filepath)

    return render_template('view.html', filename=filename, is_admin=session.get('is_admin'), is_owner=is_owner, file_type=file_record.file_type, word_content=word_content)

@app.route('/file/decrypted/<filename>')
def view_file_decrypted(filename):
    file_record = PdfFile.query.filter_by(stored_filename=filename).first()
    if not file_record:
        return "الملف غير موجود", 404

    # الحصول على المستخدم الحالي
    current_user = User.query.filter_by(username=session['username']).first()
    # التحقق مما إذا كان المستخدم هو المالك أو مسؤول
    if not (current_user.id == file_record.user_id or session.get('is_admin')):
        return "غير مسموح لك بفك تشفير هذا الملف", 403

    encrypted_filepath = os.path.join(UPLOAD_FOLDER, filename + '.enc')

    cipher = Fernet(file_record.encryption_key.encode())
    with open(encrypted_filepath, 'rb') as f:
        decrypted_data = cipher.decrypt(f.read())

    # استخدام اسم مؤقت بدون مسافات أو أحرف خاصة
    temp_filename = f"temp_{uuid.uuid4().hex}{os.path.splitext(file_record.original_filename)[1]}"
    decrypted_filepath = os.path.join(UPLOAD_FOLDER, temp_filename)
    with open(decrypted_filepath, 'wb') as f:
        f.write(decrypted_data)

    # تحديد نوع الملف لإرجاع الاستجابة المناسبة
    if file_record.file_type == 'PDF':
        response = send_file(decrypted_filepath, mimetype='application/pdf')
    elif file_record.file_type == 'Word':
        response = send_file(decrypted_filepath, as_attachment=True, download_name=file_record.original_filename)
    else:  # Image
        response = send_file(decrypted_filepath, mimetype='image/' + os.path.splitext(file_record.original_filename)[1].lstrip('.').lower())

    @response.call_on_close
    def remove_temp():
        if os.path.exists(decrypted_filepath):
            os.remove(decrypted_filepath)

    return response

@app.route('/file/encrypted/<filename>')
def view_file_encrypted(filename):
    encrypted_filepath = os.path.join(UPLOAD_FOLDER, filename + '.enc')
    try:
        with open(encrypted_filepath, 'rb') as f:
            encrypted_data = f.read()
        # تحويل البيانات المشفرة إلى سلسلة نصية (base64)
        encrypted_data_str = base64.b64encode(encrypted_data).decode('utf-8')

        # عرض أول 1000 حرف فقط
        max_display_length = 1000
        if len(encrypted_data_str) > max_display_length:
            encrypted_data_str = encrypted_data_str[:max_display_length] + "\n\n... (تم عرض أول 1000 حرف فقط من المحتوى المشفر بسبب حجمه الكبير)"

        # إنشاء ملف PDF مؤقت يحتوي على المحتوى المشفر
        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        c.setFont("Helvetica", 12)
        c.drawString(50, 750, "المحتوى المشفر (غير قابل للقراءة):")
        
        # تقسيم النص إلى أسطر لتناسب الصفحة
        y_position = 730
        text = c.beginText(50, y_position)
        text.setFont("Helvetica", 10)
        text.setLeading(14)  # المسافة بين الأسطر
        
        # تقسيم النص الطويل إلى أسطر
        line_length = 80  # عدد الأحرف في السطر
        for i in range(0, len(encrypted_data_str), line_length):
            line = encrypted_data_str[i:i + line_length]
            text.textLine(line)
            y_position -= 14
            if y_position < 50:  # إذا وصلنا إلى أسفل الصفحة، أضف صفحة جديدة
                c.drawText(text)
                c.showPage()
                c.setFont("Helvetica", 12)
                c.drawString(50, 750, "المحتوى المشفر (غير قابل للقراءة) - تابع:")
                y_position = 730
                text = c.beginText(50, y_position)
                text.setFont("Helvetica", 10)
                text.setLeading(14)
        
        c.drawText(text)
        c.showPage()
        c.save()

        # حفظ الملف المؤقت
        encrypted_pdf_path = os.path.join(UPLOAD_FOLDER, f'temp_encrypted_{filename}.pdf')
        with open(encrypted_pdf_path, 'wb') as f:
            f.write(buffer.getvalue())

        response = send_file(encrypted_pdf_path, mimetype='application/pdf')

        @response.call_on_close
        def remove_temp():
            if os.path.exists(encrypted_pdf_path):
                os.remove(encrypted_pdf_path)

        return response
    except FileNotFoundError:
        return "الملف المشفر غير موجود", 404

@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        return redirect(url_for('login'))

    # الحصول على المستخدم الحالي
    current_user = User.query.filter_by(username=session['username']).first()

    # معالجة البحث والتصفية
    search_query = request.args.get('search', '').strip()
    file_type_filter = request.args.get('file_type', '')

    # بناء الاستعلام الأساسي
    if session.get('is_admin'):
        query = PdfFile.query
    else:
        query = PdfFile.query.filter_by(user_id=current_user.id)

    # تطبيق البحث
    if search_query:
        query = query.filter(PdfFile.original_filename.ilike(f'%{search_query}%'))

    # تطبيق التصفية حسب نوع الملف
    if file_type_filter in ['PDF', 'Word', 'Image']:
        query = query.filter_by(file_type=file_type_filter)

    # الحصول على عدد الملفات
    files_count = query.count()

    # الحصول على آخر الملفات المرفوعة
    recent_files = query.order_by(PdfFile.upload_date.desc()).limit(5).all()

    users_count = User.query.count() if session.get('is_admin') else 0

    return render_template('dashboard.html', files_count=files_count, users_count=users_count, recent_files=recent_files, search_query=search_query, file_type_filter=file_type_filter)

@app.route('/manage_users')
def manage_users():
    if 'username' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))

    users = User.query.all()
    return render_template('manage_users.html', users=users)

@app.route('/delete_file/<int:file_id>', methods=['POST'])
def delete_file(file_id):
    if 'username' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))

    file_record = PdfFile.query.get_or_404(file_id)

    encrypted_filepath = os.path.join(UPLOAD_FOLDER, file_record.stored_filename + '.enc')
    qr_filepath = os.path.join(QR_FOLDER, f'{file_record.stored_filename}_qr.png')

    try:
        if os.path.exists(encrypted_filepath):
            os.remove(encrypted_filepath)

        if os.path.exists(qr_filepath):
            os.remove(qr_filepath)

        db.session.delete(file_record)
        db.session.commit()
        flash('تم حذف الملف بنجاح!', 'success')
    except Exception as e:
        flash(f'حدث خطأ أثناء حذف الملف: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))

@app.route('/add_user', methods=['GET', 'POST'])
def add_user():
    if 'username' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        is_admin = 'is_admin' in request.form

        if User.query.filter_by(username=username).first():
            return render_template('add_user.html', error="اسم المستخدم موجود بالفعل")

        hashed_password = generate_password_hash(password)
        new_user = User(username=username, password=hashed_password, is_admin=is_admin)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('manage_users'))

    return render_template('add_user.html')

@app.route('/edit_user/<int:user_id>', methods=['GET', 'POST'])
def edit_user(user_id):
    if 'username' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))

    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        user.username = request.form['username']
        if request.form['password']:
            user.password = generate_password_hash(request.form['password'])
        user.is_admin = 'is_admin' in request.form
        db.session.commit()
        return redirect(url_for('manage_users'))

    return render_template('edit_user.html', user=user)

@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    if 'username' not in session or not session.get('is_admin'):
        return redirect(url_for('login'))

    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('manage_users'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)