import os
import sys
import subprocess
import uuid
import threading
import queue
import time
import shutil
import re
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-key-flex-host-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# تهيئة قاعدة البيانات
db = SQLAlchemy(app)

# تهيئة نظام تسجيل الدخول
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '🔒 يرجى تسجيل الدخول أولاً'

# إنشاء المجلدات
os.makedirs('user_files', exist_ok=True)

# ==================== نموذج المستخدم ====================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_folder = db.Column(db.String(100), unique=True, nullable=False)
    # حفظ الجلسة النشطة
    active_task_id = db.Column(db.String(100), nullable=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_folder_path(self):
        return os.path.join('user_files', self.user_folder)

# ==================== إدارة العمليات ====================
class ProcessManager:
    def __init__(self):
        self.processes = {}
        self.outputs = {}
        self.lock = threading.Lock()
    
    def create_task(self, task_id, user_id=None):
        with self.lock:
            self.processes[task_id] = {
                'process': None,
                'running': False,
                'completed': False,
                'success': None,
                'output': [],
                'error': [],
                'start_time': None,
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
                if task_id in self.outputs:
                    self.outputs[task_id].put({'line': line, 'error': is_error})
    
    def get_status(self, task_id):
        with self.lock:
            if task_id in self.processes:
                proc = self.processes[task_id]
                return {
                    'running': proc['running'],
                    'completed': proc['completed'],
                    'success': proc['success'],
                    'output': '\n'.join(proc['output']),
                    'error': '\n'.join(proc['error']),
                    'output_list': proc['output'],
                    'error_list': proc['error'],
                    'user_id': proc.get('user_id')
                }
        return None
    
    def stop_task(self, task_id):
        with self.lock:
            if task_id in self.processes:
                proc = self.processes[task_id]
                if proc['process'] and proc['running']:
                    try:
                        proc['process'].terminate()
                        time.sleep(0.5)
                        if proc['process'].poll() is None:
                            proc['process'].kill()
                    except:
                        pass
                proc['running'] = False
                proc['completed'] = True
                proc['success'] = False
                self.add_output(task_id, '\n⛔ تم إيقاف البوت يدوياً', True)
    
    def stop_all_user_tasks(self, user_id):
        """إيقاف جميع بوتات مستخدم معين"""
        stopped = 0
        with self.lock:
            for task_id, proc in list(self.processes.items()):
                if proc.get('user_id') == user_id and proc.get('running'):
                    self.stop_task(task_id)
                    stopped += 1
        return stopped
    
    def get_user_active_tasks(self, user_id):
        """الحصول على المهام النشطة لمستخدم"""
        active = []
        with self.lock:
            for task_id, proc in self.processes.items():
                if proc.get('user_id') == user_id and proc.get('running'):
                    active.append({
                        'task_id': task_id,
                        'start_time': proc.get('start_time'),
                        'output_count': len(proc.get('output', []))
                    })
        return active

process_manager = ProcessManager()

# ==================== قاموس المكتبات ====================
LIBRARY_MAPPING = {
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
    'tensorflow': 'tensorflow',
    'torch': 'torch',
    'google.cloud': 'google-cloud',
    'google.generativeai': 'google-generativeai',
    'flask': 'flask',
    'fastapi': 'fastapi',
    'requests': 'requests',
    'aiohttp': 'aiohttp',
    'websockets': 'websockets',
    'numpy': 'numpy',
    'pandas': 'pandas',
    'pymongo': 'pymongo',
    'redis': 'redis',
    'sqlalchemy': 'sqlalchemy',
    'psycopg2': 'psycopg2-binary',
    'mysql': 'mysql-connector-python',
    'mysqldb': 'mysqlclient',
    'httpx': 'httpx',
    'httplib2': 'httplib2',
    'urllib3': 'urllib3',
    'selenium': 'selenium',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'tweepy': 'tweepy',
    'instabot': 'instabot',
    'yt-dlp': 'yt-dlp',
    'youtube_dl': 'youtube-dl',
    'openai': 'openai',
    'anthropic': 'anthropic',
    'colorama': 'colorama',
    'rich': 'rich',
    'loguru': 'loguru',
    'dotenv': 'python-dotenv',
    'pydantic': 'pydantic',
    'cryptography': 'cryptography',
    'pycryptodome': 'pycryptodome',
    'Crypto': 'pycryptodome',
    'crypto': 'pycryptodome',
    'matplotlib': 'matplotlib',
    'seaborn': 'seaborn',
    'plotly': 'plotly',
    'dash': 'dash',
    'streamlit': 'streamlit',
    'gradio': 'gradio',
    'scipy': 'scipy',
    'scikit-learn': 'scikit-learn',
    'xgboost': 'xgboost',
    'lightgbm': 'lightgbm',
    'catboost': 'catboost',
    'transformers': 'transformers',
    'datasets': 'datasets',
    'tokenizers': 'tokenizers',
    'accelerate': 'accelerate',
    'sentencepiece': 'sentencepiece',
    'protobuf': 'protobuf',
    'tiktoken': 'tiktoken',
    'langchain': 'langchain',
    'chromadb': 'chromadb',
    'faiss': 'faiss-cpu',
    'pinecone': 'pinecone-client',
    'qdrant': 'qdrant-client',
    'weaviate': 'weaviate-client',
    'lancedb': 'lancedb',
    'pypdf': 'pypdf',
    'pypdf2': 'pypdf2',
    'pdfplumber': 'pdfplumber',
    'docx': 'python-docx',
    'openpyxl': 'openpyxl',
    'xlrd': 'xlrd',
    'xlsxwriter': 'xlsxwriter',
    'csv': 'csv',
    'arrow': 'pyarrow',
    'parquet': 'pyarrow',
    'fastparquet': 'fastparquet',
    'orjson': 'orjson',
    'ujson': 'ujson',
    'msgpack': 'msgpack',
    'pyyaml': 'pyyaml',
    'toml': 'toml',
    'tomli': 'tomli',
    'tomli_w': 'tomli-w',
    'xml': 'lxml',
    'lxml': 'lxml',
    'html5lib': 'html5lib',
    'beautifulsoup4': 'beautifulsoup4',
    'scrapy': 'scrapy',
    'playwright': 'playwright',
    'asyncio': 'asyncio',
    'aiofiles': 'aiofiles',
    'aioamqp': 'aioamqp',
    'aiokafka': 'aiokafka',
    'aioredis': 'redis',
    'aiosqlite': 'aiosqlite',
    'asyncpg': 'asyncpg',
    'motor': 'motor',
    'asyncpraw': 'asyncpraw',
    'twilio': 'twilio',
    'sendgrid': 'sendgrid',
    'boto3': 'boto3',
    'botocore': 'botocore',
    'awscli': 'awscli',
    'google-cloud-storage': 'google-cloud-storage',
    'google-cloud-bigquery': 'google-cloud-bigquery',
    'google-cloud-pubsub': 'google-cloud-pubsub',
    'google-cloud-firestore': 'google-cloud-firestore',
    'azure-storage-blob': 'azure-storage-blob',
    'azure-cosmos': 'azure-cosmos',
    'azure-servicebus': 'azure-servicebus',
    'celery': 'celery',
    'kombu': 'kombu',
    'pika': 'pika',
    'kafka-python': 'kafka-python',
    'confluent-kafka': 'confluent-kafka',
    'schedule': 'schedule',
    'apscheduler': 'apscheduler',
    'croniter': 'croniter',
    'python-crontab': 'python-crontab',
    'watchdog': 'watchdog',
    'inotify': 'inotify',
    'pyinotify': 'pyinotify',
    'watchfiles': 'watchfiles',
    'uvicorn': 'uvicorn',
    'gunicorn': 'gunicorn',
    'waitress': 'waitress',
    'hypercorn': 'hypercorn',
    'daphne': 'daphne',
    'uvloop': 'uvloop',
    'socketio': 'python-socketio',
    'flask-socketio': 'flask-socketio',
    'django': 'django',
    'starlette': 'starlette',
    'sanic': 'sanic',
    'tornado': 'tornado',
    'quart': 'quart',
    'falcon': 'falcon',
    'bottle': 'bottle',
    'cherrypy': 'cherrypy',
    'pyramid': 'pyramid',
    'jinja2': 'jinja2',
    'mako': 'mako',
    'chameleon': 'chameleon',
    'cheetah': 'cheetah',
    'textual': 'textual',
    'blessed': 'blessed',
    'blessings': 'blessings',
    'click': 'click',
    'fire': 'fire',
    'typer': 'typer',
    'argparse': 'argparse',
    'docopt': 'docopt',
    'plac': 'plac',
    'cliff': 'cliff',
    'cement': 'cement',
    'prompt_toolkit': 'prompt-toolkit',
    'questionary': 'questionary',
    'inquirer': 'inquirer',
    'pygments': 'pygments',
    'tabulate': 'tabulate',
    'prettytable': 'prettytable',
    'texttable': 'texttable',
    'termcolor': 'termcolor',
    'colored': 'colored',
    'ansicolors': 'ansicolors',
    'colorlog': 'colorlog',
    'tqdm': 'tqdm',
    'progress': 'progress',
    'progressbar': 'progressbar2',
    'alive-progress': 'alive-progress',
    'halo': 'halo',
    'spinners': 'spinners',
    'yaspin': 'yaspin',
}

# ==================== دوال مساعدة ====================
def extract_imports_from_file(file_path):
    """استخراج المكتبات المطلوبة"""
    imports = set()
    standard_libs = {'os', 'sys', 'time', 'json', 're', 'math', 'random', 
                     'datetime', 'collections', 'itertools', 'threading', 
                     'subprocess', 'pathlib', 'typing', 'io', 'string', 
                     'hashlib', 'base64', 'uuid', 'logging', 'traceback',
                     'ast', 'abc', 'asyncio', 'functools', 'inspect',
                     'warnings', 'weakref', 'copy', 'enum', 'socket',
                     'ssl', 'email', 'http', 'urllib', 'xml', 'html',
                     'csv', 'configparser', 'dataclasses', 'decimal',
                     'fractions', 'statistics', 'textwrap', 'struct'}
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        import ast
        tree = ast.parse(content)
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
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
            imports.add(module.split('.')[0])
    
    return list(imports - standard_libs)

def create_venv(venv_path):
    """إنشاء بيئة افتراضية"""
    try:
        subprocess.check_call([sys.executable, '-m', 'venv', venv_path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False

def get_pip(venv_path):
    return os.path.join(venv_path, 'bin', 'pip')

def get_python(venv_path):
    return os.path.join(venv_path, 'bin', 'python')

def install_libs(venv_path, libraries, task_id):
    """تثبيت المكتبات"""
    pip = get_pip(venv_path)
    total = len(libraries)
    ok, bad = 0, []
    
    process_manager.add_output(task_id, '🔄 تحديث pip...')
    try:
        subprocess.check_call([pip, 'install', '--upgrade', 'pip'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        process_manager.add_output(task_id, '✅ تم تحديث pip')
    except:
        pass
    
    for i, lib in enumerate(libraries, 1):
        process_manager.add_output(task_id, f'📦 تثبيت ({i}/{total}): {lib}')
        actual = LIBRARY_MAPPING.get(lib, lib)
        
        try:
            result = subprocess.run([pip, 'install', actual, '--no-cache-dir'],
                                  capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                ok += 1
                process_manager.add_output(task_id, f'✅ {lib}')
            else:
                bad.append(lib)
                process_manager.add_output(task_id, f'❌ {lib}', True)
        except Exception as e:
            bad.append(lib)
            process_manager.add_output(task_id, f'❌ {lib}: {str(e)}', True)
    
    process_manager.add_output(task_id, f'\n📊 نجح: {ok}/{total}')
    if bad:
        process_manager.add_output(task_id, f'❌ فشل: {", ".join(bad)}', True)
    
    return ok, bad

def run_bot(venv_path, file_path, task_id):
    """تشغيل البوت"""
    python = get_python(venv_path)
    process_manager.add_output(task_id, '\n' + '='*50)
    process_manager.add_output(task_id, '🚀 تشغيل البوت...')
    process_manager.add_output(task_id, '='*50 + '\n')
    
    try:
        process = subprocess.Popen(
            [python, '-u', file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1, universal_newlines=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        process_manager.processes[task_id]['process'] = process
        process_manager.processes[task_id]['running'] = True
        
        def read_stdout():
            for line in iter(process.stdout.readline, ''):
                if line and process_manager.processes[task_id]['running']:
                    process_manager.add_output(task_id, line.rstrip())
        
        def read_stderr():
            for line in iter(process.stderr.readline, ''):
                if line and process_manager.processes[task_id]['running']:
                    process_manager.add_output(task_id, line.rstrip(), True)
        
        t1 = threading.Thread(target=read_stdout, daemon=True)
        t2 = threading.Thread(target=read_stderr, daemon=True)
        t1.start(); t2.start()
        
        while process.poll() is None:
            if not process_manager.processes[task_id]['running']:
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                break
            time.sleep(0.1)
        
        code = process.returncode
        process_manager.add_output(task_id, '\n' + '='*50)
        if code == 0:
            process_manager.add_output(task_id, '✅ تم بنجاح')
            process_manager.processes[task_id]['success'] = True
        else:
            process_manager.add_output(task_id, f'❌ رمز الخطأ: {code}', True)
            process_manager.processes[task_id]['success'] = False
        process_manager.add_output(task_id, '='*50)
        
    except Exception as e:
        process_manager.add_output(task_id, f'❌ خطأ: {str(e)}', True)
        process_manager.processes[task_id]['success'] = False
    
    finally:
        process_manager.processes[task_id]['completed'] = True
        process_manager.processes[task_id]['running'] = False
        # تحديث حالة المستخدم في قاعدة البيانات
        if process_manager.processes[task_id].get('user_id'):
            user = User.query.get(process_manager.processes[task_id]['user_id'])
            if user and user.active_task_id == task_id:
                user.active_task_id = None
                db.session.commit()

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
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
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
            flash('❌ يجب أن يكون البريد الإلكتروني تابعاً لـ @flex.host', 'error')
            return render_template('register.html')
        
        if password != confirm:
            flash('❌ كلمات المرور غير متطابقة', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('❌ كلمة المرور يجب أن تكون 6 أحرف على الأقل', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('❌ هذا البريد الإلكتروني مستخدم بالفعل', 'error')
            return render_template('register.html')
        
        folder_name = str(uuid.uuid4())[:16]
        user = User(email=email, user_folder=folder_name)
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        os.makedirs(user.get_folder_path(), exist_ok=True)
        
        flash('✅ تم إنشاء الحساب بنجاح! يمكنك الآن تسجيل الدخول', 'success')
        return redirect(url_for('login'))
    
    random_email = f"user{uuid.uuid4().hex[:8]}@flex.host"
    return render_template('register.html', random_email=random_email)

@app.route('/dashboard')
@login_required
def dashboard():
    # جلب الجلسة النشطة للمستخدم
    active_task_id = current_user.active_task_id
    active_task_status = None
    
    if active_task_id:
        active_task_status = process_manager.get_status(active_task_id)
        # إذا البوت خلص، نحذف الجلسة
        if active_task_status and active_task_status['completed']:
            current_user.active_task_id = None
            db.session.commit()
            active_task_id = None
            active_task_status = None
    
    return render_template('dashboard.html', 
                         user=current_user,
                         active_task_id=active_task_id,
                         active_task_status=active_task_status)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('👋 تم تسجيل الخروج بنجاح', 'info')
    return redirect(url_for('login'))

@app.route('/stop-all', methods=['POST'])
@login_required
def stop_all():
    """إيقاف جميع بوتات المستخدم الحالي"""
    stopped = process_manager.stop_all_user_tasks(current_user.id)
    
    # تحديث قاعدة البيانات
    if current_user.active_task_id:
        current_user.active_task_id = None
        db.session.commit()
    
    return jsonify({
        'message': f'تم إيقاف {stopped} بوت بنجاح',
        'stopped': stopped
    })

@app.route('/active-tasks', methods=['GET'])
@login_required
def active_tasks():
    """جلب المهام النشطة للمستخدم"""
    tasks = process_manager.get_user_active_tasks(current_user.id)
    return jsonify({'tasks': tasks, 'count': len(tasks)})

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'لم يتم رفع أي ملف'}), 400
    
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.py'):
        return jsonify({'error': 'يجب رفع ملف Python'}), 400
    
    task_id = str(uuid.uuid4())
    user_folder = current_user.get_folder_path()
    
    file_path = os.path.join(user_folder, f"{task_id}_{secure_filename(file.filename)}")
    file.save(file_path)
    
    venv_path = os.path.join(user_folder, f'venv_{task_id}')
    process_manager.create_task(task_id, current_user.id)
    
    if not create_venv(venv_path):
        return jsonify({'error': 'فشل إنشاء البيئة'}), 500
    
    # حفظ الجلسة النشطة في قاعدة البيانات
    current_user.active_task_id = task_id
    db.session.commit()
    
    process_manager.add_output(task_id, '🔍 تحليل المكتبات...')
    libs = extract_imports_from_file(file_path)
    
    if libs:
        process_manager.add_output(task_id, f'📚 المكتبات: {", ".join(libs)}')
    
    auto_run = request.form.get('auto_run', 'true') == 'true'
    
    threading.Thread(target=process_and_run, 
                   args=(task_id, venv_path, file_path, libs, auto_run), 
                   daemon=True).start()
    
    return jsonify({
        'task_id': task_id,
        'message': 'تم رفع الملف بنجاح',
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
            process_manager.processes[task_id]['completed'] = True
            process_manager.processes[task_id]['success'] = True
    except Exception as e:
        process_manager.add_output(task_id, f'\n❌ خطأ: {str(e)}', True)
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
    
    process_manager.create_task(task_id, current_user.id)
    
    # تحديث الجلسة النشطة
    current_user.active_task_id = task_id
    db.session.commit()
    
    threading.Thread(target=run_bot, args=(venv_path, str(py_files[0]), task_id), daemon=True).start()
    
    return jsonify({'message': 'تم التشغيل'})

@app.route('/stop/<task_id>', methods=['POST'])
@login_required
def stop_task(task_id):
    process_manager.stop_task(task_id)
    
    if current_user.active_task_id == task_id:
        current_user.active_task_id = None
        db.session.commit()
    
    return jsonify({'message': 'تم الإيقاف'})

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
            errors = status['error_list']
            
            while last < len(outputs):
                yield f"data: {json.dumps({'type': 'output', 'text': outputs[last], 'running': status['running'], 'completed': status['completed']})}\n\n"
                last += 1
            
            for err in errors:
                yield f"data: {json.dumps({'type': 'error', 'text': err})}\n\n"
            
            if status['completed']:
                yield f"data: {json.dumps({'type': 'complete', 'success': status['success'], 'running': False, 'completed': True})}\n\n"
                break
            
            time.sleep(0.5)
    
    return Response(generate(), mimetype='text/event-stream')

# ==================== إنشاء قاعدة البيانات ====================
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
