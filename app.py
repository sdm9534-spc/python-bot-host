import os
import sys
import subprocess
import uuid
import threading
import queue
import time
import shutil
import re
import signal
import atexit
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json

# ==================== تنظيف عند الخروج ====================
def cleanup_on_exit():
    """تنظيف جميع العمليات عند إغلاق السيرفر"""
    for task_id, proc_data in list(process_manager_ref.get('processes', {}).items()):
        process = proc_data.get('process')
        if process and proc_data.get('running'):
            try:
                process.terminate()
                time.sleep(0.3)
                if process.poll() is None:
                    process.kill()
            except:
                pass

# ==================== إعداد التطبيق ====================
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'flex-host-ultra-secure-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '🔒 يرجى تسجيل الدخول أولاً'

os.makedirs('user_files', exist_ok=True)
os.makedirs('uploads', exist_ok=True)

# ==================== نموذج المستخدم ====================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_folder = db.Column(db.String(100), unique=True, nullable=False)
    active_task_id = db.Column(db.String(100), nullable=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_folder_path(self):
        return os.path.join('user_files', self.user_folder)

# ==================== إدارة العمليات (نسخة محسنة) ====================
class ProcessManager:
    def __init__(self):
        self.processes = {}
        self.outputs = {}
        self.lock = threading.RLock()  # Reentrant lock for better thread safety
    
    def create_task(self, task_id, user_id=None):
        with self.lock:
            self.processes[task_id] = {
                'process': None,
                'running': False,
                'completed': False,
                'success': None,
                'output': [],
                'error': [],
                'start_time': time.time(),
                'user_id': user_id
            }
            self.outputs[task_id] = queue.Queue()
    
    def add_output(self, task_id, line, is_error=False):
        with self.lock:
            if task_id in self.processes:
                if is_error:
                    self.processes[task_id]['error'].append(line)
                else:
                    self.processes[task_id]['output'].append(line)
                try:
                    self.outputs[task_id].put_nowait({'line': line, 'error': is_error})
                except:
                    pass
    
    def get_status(self, task_id):
        with self.lock:
            if task_id in self.processes:
                proc = self.processes[task_id]
                return {
                    'running': proc['running'],
                    'completed': proc['completed'],
                    'success': proc['success'],
                    'output': '\n'.join(proc['output'][-500:]),  # آخر 500 سطر
                    'error': '\n'.join(proc['error'][-100:]),
                    'output_list': proc['output'][-500:],
                    'error_list': proc['error'][-100:],
                    'user_id': proc.get('user_id')
                }
        return None
    
    def force_kill_process(self, task_id):
        """قتل العملية بالقوة - بدون رحمة"""
        with self.lock:
            if task_id not in self.processes:
                return False
            
            proc_data = self.processes[task_id]
            process = proc_data.get('process')
            
            killed = False
            
            # الطريقة 1: terminate ثم kill
            if process:
                try:
                    process.terminate()
                    time.sleep(0.3)
                    if process.poll() is None:
                        process.kill()
                        time.sleep(0.2)
                    killed = True
                except:
                    pass
                
                # الطريقة 2: SIGKILL مباشر
                if process and process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                        killed = True
                    except:
                        pass
            
            # الطريقة 3: قتل باستخدام psutil
            if not killed:
                try:
                    import psutil
                    current_pid = os.getpid()
                    for proc in psutil.process_iter(['pid', 'cmdline', 'ppid']):
                        try:
                            cmdline = ' '.join(proc.info.get('cmdline', []))
                            pid = proc.info['pid']
                            ppid = proc.info['ppid']
                            # تجاهل العملية الحالية وعمليات النظام
                            if pid == current_pid or ppid == current_pid:
                                continue
                            if task_id in cmdline:
                                os.kill(pid, signal.SIGKILL)
                                killed = True
                        except:
                            pass
                except:
                    pass
            
            # تحديث الحالة
            proc_data['running'] = False
            proc_data['completed'] = True
            proc_data['success'] = False
            proc_data['process'] = None
            
            self.add_output(task_id, '\n⛔ تم إيقاف البوت بالقوة', True)
            
            return killed
    
    def stop_task(self, task_id):
        """إيقاف مهمة مع تنظيف كامل"""
        self.force_kill_process(task_id)
    
    def stop_all_user_tasks(self, user_id):
        """إيقاف جميع مهام مستخدم"""
        stopped = 0
        with self.lock:
            for task_id in list(self.processes.keys()):
                if self.processes[task_id].get('user_id') == user_id:
                    if self.processes[task_id].get('running'):
                        self.force_kill_process(task_id)
                        stopped += 1
        return stopped
    
    def cleanup_old_tasks(self, max_age=3600):
        """تنظيف المهام القديمة (أكبر من ساعة)"""
        now = time.time()
        with self.lock:
            for task_id in list(self.processes.keys()):
                proc = self.processes[task_id]
                if not proc['running'] and (now - proc['start_time']) > max_age:
                    self.processes.pop(task_id, None)
                    self.outputs.pop(task_id, None)

process_manager = ProcessManager()

# مرجع عام للتنظيف
process_manager_ref = {
    'processes': process_manager.processes
}

# تسجيل دالة التنظيف
atexit.register(cleanup_on_exit)

# ==================== قاموس المكتبات الموسع ====================
LIBRARY_MAPPING = {
    # المكتبات الأساسية
    'telegram': 'python-telegram-bot',
    'telegram.ext': 'python-telegram-bot',
    'telegram.update': 'python-telegram-bot',
    'discord': 'discord.py',
    'discord.ext': 'discord.py',
    'PIL': 'pillow',
    'pil': 'pillow',
    'bs4': 'beautifulsoup4',
    'cv2': 'opencv-python',
    'sklearn': 'scikit-learn',
    'Crypto': 'pycryptodome',
    'crypto': 'pycryptodome',
    'mysql': 'mysql-connector-python',
    'mysqldb': 'mysqlclient',
    'yaml': 'pyyaml',
    'dotenv': 'python-dotenv',
    'google.generativeai': 'google-generativeai',
    'google.cloud': 'google-cloud',
    
    # المكتبات المشهورة
    'flask': 'flask',
    'fastapi': 'fastapi',
    'django': 'django',
    'requests': 'requests',
    'aiohttp': 'aiohttp',
    'httpx': 'httpx',
    'websockets': 'websockets',
    'numpy': 'numpy',
    'pandas': 'pandas',
    'matplotlib': 'matplotlib',
    'seaborn': 'seaborn',
    'plotly': 'plotly',
    'scipy': 'scipy',
    'tensorflow': 'tensorflow',
    'torch': 'torch',
    'transformers': 'transformers',
    'openai': 'openai',
    'anthropic': 'anthropic',
    'langchain': 'langchain',
    'pymongo': 'pymongo',
    'redis': 'redis',
    'sqlalchemy': 'sqlalchemy',
    'psycopg2': 'psycopg2-binary',
    'selenium': 'selenium',
    'playwright': 'playwright',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'tweepy': 'tweepy',
    'yt-dlp': 'yt-dlp',
    'youtube_dl': 'youtube-dl',
    'colorama': 'colorama',
    'rich': 'rich',
    'tqdm': 'tqdm',
    'pydantic': 'pydantic',
    'cryptography': 'cryptography',
    'uvicorn': 'uvicorn',
    'gunicorn': 'gunicorn',
    'boto3': 'boto3',
    'celery': 'celery',
    'dash': 'dash',
    'streamlit': 'streamlit',
    'gradio': 'gradio',
    'scrapy': 'scrapy',
    'jinja2': 'jinja2',
    'click': 'click',
    'typer': 'typer',
    'loguru': 'loguru',
    'pypdf': 'pypdf',
    'pypdf2': 'pypdf2',
    'docx': 'python-docx',
    'openpyxl': 'openpyxl',
    'xlrd': 'xlrd',
    'lxml': 'lxml',
    'beautifulsoup4': 'beautifulsoup4',
    'schedule': 'schedule',
    'apscheduler': 'apscheduler',
    'watchdog': 'watchdog',
    'pygments': 'pygments',
    'tabulate': 'tabulate',
    'python-dotenv': 'python-dotenv',
}

# المكتبات القياسية في Python
STANDARD_LIBS = {
    'os', 'sys', 'time', 'json', 're', 'math', 'random', 'datetime',
    'collections', 'itertools', 'functools', 'threading', 'subprocess',
    'pathlib', 'typing', 'io', 'string', 'hashlib', 'base64', 'uuid',
    'logging', 'traceback', 'ast', 'abc', 'asyncio', 'inspect', 'warnings',
    'weakref', 'copy', 'enum', 'socket', 'ssl', 'email', 'http', 'urllib',
    'xml', 'html', 'csv', 'configparser', 'dataclasses', 'decimal',
    'fractions', 'statistics', 'textwrap', 'struct', 'signal', 'atexit',
    'shutil', 'glob', 'fnmatch', 'tempfile', 'zipfile', 'tarfile',
    'argparse', 'getpass', 'getopt', 'operator', 'pprint', 'pickle',
    'sqlite3', 'unittest', 'doctest', 'profile', 'cProfile', 'contextlib',
    'importlib', 'pkgutil', 'venv', 'platform', 'ctypes', 'multiprocessing',
    'concurrent', 'queue', 'asyncio', '_thread', '__future__'
}

# ==================== دوال مساعدة ====================
def extract_imports_from_file(file_path):
    """استخراج المكتبات المطلوبة من ملف Python"""
    imports = set()
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        import ast as ast_module
        tree = ast_module.parse(content)
        
        for node in ast_module.walk(tree):
            if isinstance(node, ast_module.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast_module.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])
    except:
        import re
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        import_pattern = r'(?:from\s+(\S+)\s+import|import\s+(\S+))'
        matches = re.findall(import_pattern, content)
        for match in matches:
            module = match[0] or match[1]
            if module:
                imports.add(module.split('.')[0])
    
    return list(imports - STANDARD_LIBS)

def create_venv(venv_path):
    """إنشاء بيئة افتراضية"""
    try:
        subprocess.check_call([sys.executable, '-m', 'venv', venv_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return True
    except:
        return False

def get_pip(venv_path):
    return os.path.join(venv_path, 'bin', 'pip')

def get_python(venv_path):
    return os.path.join(venv_path, 'bin', 'python')

def install_libs(venv_path, libraries, task_id):
    """تثبيت المكتبات بسرعة"""
    pip = get_pip(venv_path)
    total = len(libraries)
    ok, bad = 0, []
    
    process_manager.add_output(task_id, '🔄 تحديث pip...')
    try:
        subprocess.run([pip, 'install', '--upgrade', 'pip', '--quiet'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        process_manager.add_output(task_id, '✅ تم تحديث pip')
    except:
        process_manager.add_output(task_id, '⚠️ تعذر تحديث pip')
    
    for i, lib in enumerate(libraries, 1):
        process_manager.add_output(task_id, f'📦 ({i}/{total}) {lib}')
        actual = LIBRARY_MAPPING.get(lib, lib)
        
        try:
            result = subprocess.run([pip, 'install', actual, '--no-cache-dir', '--quiet'],
                                  capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                ok += 1
                process_manager.add_output(task_id, f'✅ {lib}')
            else:
                # محاولة ثانية بدون quiet لرؤية الخطأ
                result2 = subprocess.run([pip, 'install', actual, '--no-cache-dir'],
                                       capture_output=True, text=True, timeout=60)
                if result2.returncode == 0:
                    ok += 1
                    process_manager.add_output(task_id, f'✅ {lib}')
                else:
                    bad.append(lib)
                    error_msg = result2.stderr.split('\n')[-2] if result2.stderr else 'Unknown error'
                    process_manager.add_output(task_id, f'❌ {lib}: {error_msg[:100]}', True)
        except subprocess.TimeoutExpired:
            bad.append(lib)
            process_manager.add_output(task_id, f'⏰ {lib}: timeout', True)
        except Exception as e:
            bad.append(lib)
            process_manager.add_output(task_id, f'❌ {lib}: {str(e)[:100]}', True)
    
    process_manager.add_output(task_id, f'\n📊 نجح: {ok}/{total}' + (f' | فشل: {len(bad)}' if bad else ''))
    return ok, bad

def run_bot(venv_path, file_path, task_id):
    """تشغيل البوت في بيئة معزولة"""
    python = get_python(venv_path)
    
    process_manager.add_output(task_id, '\n' + '='*50)
    process_manager.add_output(task_id, '🚀 بدء تشغيل البوت...')
    process_manager.add_output(task_id, f'📂 {os.path.basename(file_path)}')
    process_manager.add_output(task_id, '='*50 + '\n')
    
    try:
        process = subprocess.Popen(
            [python, '-u', file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1, universal_newlines=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'},
            preexec_fn=os.setsid if os.name != 'nt' else None  # مجموعة عمليات منفصلة
        )
        
        with process_manager.lock:
            if task_id in process_manager.processes:
                process_manager.processes[task_id]['process'] = process
                process_manager.processes[task_id]['running'] = True
                process_manager.processes[task_id]['start_time'] = time.time()
        
        def read_stream(stream, is_error=False):
            for line in iter(stream.readline, ''):
                if not line:
                    break
                with process_manager.lock:
                    if task_id not in process_manager.processes:
                        break
                    if not process_manager.processes[task_id].get('running', False):
                        break
                process_manager.add_output(task_id, line.rstrip(), is_error)
        
        t1 = threading.Thread(target=read_stream, args=(process.stdout, False), daemon=True)
        t2 = threading.Thread(target=read_stream, args=(process.stderr, True), daemon=True)
        t1.start()
        t2.start()
        
        # انتظار مع مراقبة
        while process.poll() is None:
            time.sleep(0.2)
            with process_manager.lock:
                if task_id not in process_manager.processes:
                    break
                if not process_manager.processes[task_id].get('running', False):
                    process.terminate()
                    time.sleep(0.3)
                    if process.poll() is None:
                        process.kill()
                    break
        
        t1.join(timeout=3)
        t2.join(timeout=3)
        
        return_code = process.returncode
        
        process_manager.add_output(task_id, '\n' + '='*50)
        if return_code == 0:
            process_manager.add_output(task_id, '✅ انتهى البوت بنجاح')
            with process_manager.lock:
                if task_id in process_manager.processes:
                    process_manager.processes[task_id]['success'] = True
        elif return_code is None:
            process_manager.add_output(task_id, '⛔ تم إيقاف البوت', True)
            with process_manager.lock:
                if task_id in process_manager.processes:
                    process_manager.processes[task_id]['success'] = False
        else:
            process_manager.add_output(task_id, f'❌ خرج البوت مع رمز: {return_code}', True)
            with process_manager.lock:
                if task_id in process_manager.processes:
                    process_manager.processes[task_id]['success'] = False
        process_manager.add_output(task_id, '='*50)
        
    except Exception as e:
        process_manager.add_output(task_id, f'\n❌ خطأ في التشغيل: {str(e)}', True)
        with process_manager.lock:
            if task_id in process_manager.processes:
                process_manager.processes[task_id]['success'] = False
    
    finally:
        with process_manager.lock:
            if task_id in process_manager.processes:
                process_manager.processes[task_id]['running'] = False
                process_manager.processes[task_id]['completed'] = True
                process_manager.processes[task_id]['process'] = None
        
        # تحديث قاعدة البيانات
        try:
            with app.app_context():
                users = User.query.filter_by(active_task_id=task_id).all()
                for user in users:
                    user.active_task_id = None
                db.session.commit()
        except:
            pass

# ==================== Flask-Login ====================
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==================== Routes ====================
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):
            login_user(user, remember=True)
            flash('✅ تم تسجيل الدخول بنجاح!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('❌ البريد الإلكتروني أو كلمة المرور غير صحيحة', 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        
        if not email.endswith('@flex.host'):
            flash('❌ يجب أن يكون البريد الإلكتروني @flex.host', 'error')
            return render_template('register.html')
        
        if password != confirm:
            flash('❌ كلمات المرور غير متطابقة', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('❌ كلمة المرور 6 أحرف على الأقل', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('❌ البريد مستخدم بالفعل', 'error')
            return render_template('register.html')
        
        folder_name = str(uuid.uuid4())[:16]
        user = User(email=email, user_folder=folder_name)
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        os.makedirs(user.get_folder_path(), exist_ok=True)
        
        flash('✅ تم إنشاء الحساب بنجاح!', 'success')
        return redirect(url_for('login'))
    
    random_email = f"user{uuid.uuid4().hex[:8]}@flex.host"
    return render_template('register.html', random_email=random_email)

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user)

@app.route('/logout')
@login_required
def logout():
    # إيقاف بوتات المستخدم قبل الخروج
    process_manager.stop_all_user_tasks(current_user.id)
    logout_user()
    flash('👋 تم تسجيل الخروج', 'info')
    return redirect(url_for('login'))

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'لم يتم رفع ملف'}), 400
    
    file = request.files['file']
    if not file.filename or not file.filename.endswith('.py'):
        return jsonify({'error': 'ملف Python فقط'}), 400
    
    task_id = str(uuid.uuid4())
    user_folder = current_user.get_folder_path()
    
    file_path = os.path.join(user_folder, f"{task_id}_{secure_filename(file.filename)}")
    file.save(file_path)
    
    venv_path = os.path.join(user_folder, f'venv_{task_id}')
    process_manager.create_task(task_id, current_user.id)
    
    if not create_venv(venv_path):
        return jsonify({'error': 'فشل إنشاء البيئة'}), 500
    
    # حفظ الجلسة
    current_user.active_task_id = task_id
    db.session.commit()
    
    process_manager.add_output(task_id, '🔍 تحليل المكتبات...')
    libs = extract_imports_from_file(file_path)
    
    if libs:
        process_manager.add_output(task_id, f'📚 المكتبات: {", ".join(libs)}')
    else:
        process_manager.add_output(task_id, 'ℹ️ لا توجد مكتبات خارجية')
    
    auto_run = request.form.get('auto_run', 'true') == 'true'
    
    threading.Thread(target=process_and_run, 
                   args=(task_id, venv_path, file_path, libs, auto_run), 
                   daemon=True).start()
    
    return jsonify({
        'task_id': task_id,
        'message': 'تم رفع الملف',
        'filename': file.filename,
        'libraries': libs
    })

def process_and_run(task_id, venv_path, file_path, libs, auto_run):
    try:
        if libs:
            process_manager.add_output(task_id, '\n📥 تثبيت المكتبات...')
            process_manager.add_output(task_id, '-'*40)
            install_libs(venv_path, libs, task_id)
            process_manager.add_output(task_id, '-'*40)
        
        if auto_run:
            run_bot(venv_path, file_path, task_id)
        else:
            process_manager.add_output(task_id, '\n✅ جاهز. اضغط تشغيل للبدء')
            with process_manager.lock:
                if task_id in process_manager.processes:
                    process_manager.processes[task_id]['completed'] = True
                    process_manager.processes[task_id]['success'] = True
    except Exception as e:
        process_manager.add_output(task_id, f'\n❌ خطأ: {str(e)}', True)
        with process_manager.lock:
            if task_id in process_manager.processes:
                process_manager.processes[task_id]['completed'] = True
                process_manager.processes[task_id]['success'] = False

@app.route('/run/<task_id>', methods=['POST'])
@login_required
def run_task(task_id):
    status = process_manager.get_status(task_id)
    if not status:
        return jsonify({'error': 'المهمة غير موجودة'}), 404
    if status['running']:
        return jsonify({'error': 'البوت يعمل بالفعل'}), 400
    
    user_folder = current_user.get_folder_path()
    py_files = list(Path(user_folder).glob(f'{task_id}_*.py'))
    if not py_files:
        return jsonify({'error': 'الملف غير موجود'}), 404
    
    venv_path = os.path.join(user_folder, f'venv_{task_id}')
    
    with process_manager.lock:
        if task_id in process_manager.processes:
            process_manager.processes[task_id]['running'] = False
            process_manager.processes[task_id]['completed'] = False
    
    process_manager.create_task(task_id, current_user.id)
    current_user.active_task_id = task_id
    db.session.commit()
    
    threading.Thread(target=run_bot, args=(venv_path, str(py_files[0]), task_id), daemon=True).start()
    
    return jsonify({'message': 'تم التشغيل'})

@app.route('/stop/<task_id>', methods=['POST'])
@login_required
def stop_task(task_id):
    """إيقاف البوت بالقوة - النسخة النووية 💣"""
    
    # 1- إيقاف من ProcessManager
    process_manager.stop_task(task_id)
    
    # 2- قتل العملية مباشرة
    with process_manager.lock:
        if task_id in process_manager.processes:
            proc_data = process_manager.processes[task_id]
            process = proc_data.get('process')
            if process:
                # محاولة 1: terminate
                try:
                    process.terminate()
                    time.sleep(0.5)
                except:
                    pass
                
                # محاولة 2: kill
                if process.poll() is None:
                    try:
                        process.kill()
                        time.sleep(0.3)
                    except:
                        pass
                
                # محاولة 3: SIGKILL مباشر
                if process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                    except:
                        pass
                
                proc_data['process'] = None
            
            proc_data['running'] = False
            proc_data['completed'] = True
            proc_data['success'] = False
    
    # 3- قتل أي عمليات متعلقة بالـ task_id
    try:
        import psutil
        current_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'cmdline', 'ppid']):
            try:
                pid = proc.info['pid']
                ppid = proc.info.get('ppid')
                cmdline = ' '.join(proc.info.get('cmdline', []))
                
                # تجاهل العملية الحالية
                if pid == current_pid or ppid == current_pid:
                    continue
                
                if task_id in cmdline:
                    os.kill(pid, signal.SIGKILL)
            except:
                pass
    except:
        pass
    
    # 4- تحديث قاعدة البيانات
    try:
        if current_user.active_task_id == task_id:
            current_user.active_task_id = None
            db.session.commit()
    except:
        pass
    
    return jsonify({'message': '✅ تم إيقاف البوت بالقوة', 'stopped': True})

@app.route('/stop-all', methods=['POST'])
@login_required
def stop_all():
    """إيقاف جميع بوتات المستخدم"""
    stopped = process_manager.stop_all_user_tasks(current_user.id)
    
    if current_user.active_task_id:
        current_user.active_task_id = None
        db.session.commit()
    
    return jsonify({
        'message': f'✅ تم إيقاف {stopped} بوت',
        'stopped': stopped
    })

@app.route('/status/<task_id>')
@login_required
def get_status(task_id):
    status = process_manager.get_status(task_id)
    if not status:
        return jsonify({'error': 'غير موجود'}), 404
    return jsonify(status)

@app.route('/output/<task_id>')
@login_required
def output_stream(task_id):
    def generate():
        if task_id not in process_manager.outputs:
            yield f"data: {json.dumps({'error': 'غير موجود'})}\n\n"
            return
        
        last = 0
        while True:
            status = process_manager.get_status(task_id)
            if not status:
                break
            
            outputs = status['output_list']
            
            while last < len(outputs):
                yield f"data: {json.dumps({'type': 'output', 'text': outputs[last], 'running': status['running'], 'completed': status['completed']})}\n\n"
                last += 1
            
            if status['completed']:
                yield f"data: {json.dumps({'type': 'complete', 'success': status['success'], 'running': False, 'completed': True})}\n\n"
                break
            
            time.sleep(0.3)
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/cleanup/<task_id>', methods=['POST'])
@login_required
def cleanup(task_id):
    process_manager.force_kill_process(task_id)
    
    task_dir = os.path.join(current_user.get_folder_path(), f'{task_id}_*')
    for f in Path(current_user.get_folder_path()).glob(f'{task_id}_*'):
        try:
            if f.is_file():
                f.unlink()
        except:
            pass
    
    venv_path = os.path.join(current_user.get_folder_path(), f'venv_{task_id}')
    if os.path.exists(venv_path):
        try:
            shutil.rmtree(venv_path, ignore_errors=True)
        except:
            pass
    
    with process_manager.lock:
        process_manager.processes.pop(task_id, None)
        process_manager.outputs.pop(task_id, None)
    
    return jsonify({'message': 'تم التنظيف'})

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'active_tasks': len([p for p in process_manager.processes.values() if p['running']]),
        'uptime': time.time()
    })

# ==================== إنشاء قاعدة البيانات ====================
with app.app_context():
    db.create_all()

# ==================== تشغيل ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
