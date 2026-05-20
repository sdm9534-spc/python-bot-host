import os
import sys
import subprocess
import uuid
import threading
import queue
import time
import shutil
import signal
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_file
from werkzeug.utils import secure_filename
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'supersecretkey123456789')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# إنشاء المجلدات المطلوبة
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('temp', exist_ok=True)
os.makedirs('venvs', exist_ok=True)

class ProcessManager:
    def __init__(self):
        self.processes = {}
        self.outputs = {}
        self.venvs = {}
        self.lock = threading.Lock()
    
    def create_task(self, task_id):
        with self.lock:
            self.processes[task_id] = {
                'process': None,
                'running': False,
                'completed': False,
                'success': None,
                'output': [],
                'error': [],
                'start_time': None,
                'end_time': None,
                'venv_path': None
            }
            self.outputs[task_id] = queue.Queue()
    
    def update_process(self, task_id, **kwargs):
        with self.lock:
            if task_id in self.processes:
                self.processes[task_id].update(kwargs)
    
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
                    'error_list': proc['error']
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

process_manager = ProcessManager()

def extract_imports_from_file(file_path):
    """استخراج المكتبات المطلوبة من ملف Python"""
    imports = set()
    
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # استخراج المكتبات باستخدام ast
        import ast
        tree = ast.parse(content)
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split('.')[0]
                    imports.add(module)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    module = node.module.split('.')[0]
                    imports.add(module)
    except:
        # إذا فشل الـ ast parsing، نستخدم طريقة regex
        import re
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # البحث عن import و from ... import
        import_pattern = r'(?:from\s+(\S+)\s+import|import\s+(\S+))'
        matches = re.findall(import_pattern, content)
        for match in matches:
            module = match[0] or match[1]
            module = module.split('.')[0]
            imports.add(module)
    
    # إزالة المكتبات القياسية
    standard_libs = {'os', 'sys', 'time', 'json', 're', 'math', 'random', 
                     'datetime', 'collections', 'itertools', 'functools', 
                     'threading', 'subprocess', 'pathlib', 'typing', 'io',
                     'string', 'hashlib', 'base64', 'uuid', 'logging', 'traceback'}
    
    third_party = imports - standard_libs
    
    return list(third_party)

def create_virtual_env(venv_path):
    """إنشاء بيئة افتراضية جديدة"""
    try:
        subprocess.check_call([sys.executable, '-m', 'venv', venv_path], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except:
        return False

def get_pip_path(venv_path):
    """الحصول على مسار pip في البيئة الافتراضية"""
    if os.name == 'nt':  # Windows
        return os.path.join(venv_path, 'Scripts', 'pip.exe')
    return os.path.join(venv_path, 'bin', 'pip')

def get_python_path(venv_path):
    """الحصول على مسار Python في البيئة الافتراضية"""
    if os.name == 'nt':  # Windows
        return os.path.join(venv_path, 'Scripts', 'python.exe')
    return os.path.join(venv_path, 'bin', 'python')

def install_libraries(venv_path, libraries, task_id):
    """تثبيت جميع المكتبات المطلوبة في البيئة الافتراضية"""
    pip_path = get_pip_path(venv_path)
    total = len(libraries)
    successful = 0
    failed = []
    
    # تحديث pip أولاً
    process_manager.add_output(task_id, '🔄 جاري تحديث pip...')
    try:
        subprocess.check_call([pip_path, 'install', '--upgrade', 'pip'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        process_manager.add_output(task_id, '✅ تم تحديث pip بنجاح')
    except:
        process_manager.add_output(task_id, '⚠️ فشل تحديث pip، نكمل على أي حال')
    
    for i, lib in enumerate(libraries, 1):
        process_manager.add_output(task_id, f'📦 جاري تثبيت المكتبة ({i}/{total}): {lib}')
        
        try:
            # محاولة تثبيت المكتبة
            result = subprocess.run([pip_path, 'install', lib, '--no-cache-dir'],
                                  capture_output=True, text=True, timeout=120)
            
            if result.returncode == 0:
                successful += 1
                process_manager.add_output(task_id, f'✅ تم تثبيت {lib} بنجاح')
            else:
                # محاولة تثبيت باسم مختلف (إضافة -py أو بدون .py)
                alt_name = lib.replace('.py', '')
                try:
                    subprocess.check_call([pip_path, 'install', alt_name, '--no-cache-dir'],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                    successful += 1
                    process_manager.add_output(task_id, f'✅ تم تثبيت {lib} (باسم {alt_name})')
                except:
                    failed.append(lib)
                    process_manager.add_output(task_id, f'❌ فشل تثبيت {lib}', True)
                    
        except subprocess.TimeoutExpired:
            failed.append(lib)
            process_manager.add_output(task_id, f'⏰ انتهت مهلة تثبيت {lib}', True)
        except Exception as e:
            failed.append(lib)
            process_manager.add_output(task_id, f'❌ خطأ في تثبيت {lib}: {str(e)}', True)
    
    process_manager.add_output(task_id, f'\n📊 إحصائيات التثبيت:')
    process_manager.add_output(task_id, f'✅ تم بنجاح: {successful}/{total}')
    if failed:
        process_manager.add_output(task_id, f'❌ فشل: {", ".join(failed)}', True)
    
    return successful, failed

def run_bot(venv_path, file_path, task_id):
    """تشغيل ملف البوت في البيئة الافتراضية"""
    python_path = get_python_path(venv_path)
    
    process_manager.update_process(task_id, start_time=time.time())
    process_manager.add_output(task_id, '\n' + '='*50)
    process_manager.add_output(task_id, '🚀 جاري تشغيل البوت...')
    process_manager.add_output(task_id, '='*50 + '\n')
    
    try:
        # تشغيل العملية مع توجيه stdout و stderr
        process = subprocess.Popen(
            [python_path, '-u', file_path],  # -u للـ unbuffered output
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        process_manager.update_process(task_id, process=process, running=True)
        
        # قراءة المخرجات في Threads منفصلة
        def read_stdout():
            for line in iter(process.stdout.readline, ''):
                if line:
                    process_manager.add_output(task_id, line.rstrip())
                if not process_manager.processes[task_id]['running']:
                    process.terminate()
                    break
        
        def read_stderr():
            for line in iter(process.stderr.readline, ''):
                if line:
                    process_manager.add_output(task_id, line.rstrip(), True)
        
        stdout_thread = threading.Thread(target=read_stdout)
        stderr_thread = threading.Thread(target=read_stderr)
        stdout_thread.daemon = True
        stderr_thread.daemon = True
        stdout_thread.start()
        stderr_thread.start()
        
        # انتظار انتهاء العملية أو إيقافها
        while process.poll() is None:
            if not process_manager.processes[task_id]['running']:
                process.terminate()
                time.sleep(0.5)
                if process.poll() is None:
                    process.kill()
                break
            time.sleep(0.1)
        
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        
        return_code = process.returncode
        
        process_manager.add_output(task_id, '\n' + '='*50)
        if return_code == 0:
            process_manager.add_output(task_id, '✅ تم انتهاء تشغيل البوت بنجاح')
            process_manager.update_process(task_id, success=True)
        else:
            process_manager.add_output(task_id, f'❌ انتهى البوت مع رمز خطأ: {return_code}', True)
            process_manager.update_process(task_id, success=False)
        process_manager.add_output(task_id, '='*50)
        
    except Exception as e:
        process_manager.add_output(task_id, f'❌ خطأ في تشغيل البوت: {str(e)}', True)
        process_manager.update_process(task_id, success=False)
    
    finally:
        process_manager.update_process(task_id, 
                                     completed=True,
                                     running=False,
                                     end_time=time.time())

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'لم يتم رفع أي ملف'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'الملف فارغ'}), 400
    
    if not file.filename.endswith('.py'):
        return jsonify({'error': 'يجب رفع ملف Python بصيغة .py فقط'}), 400
    
    # حفظ الملف
    task_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    task_dir = os.path.join(app.config['UPLOAD_FOLDER'], task_id)
    os.makedirs(task_dir, exist_ok=True)
    
    file_path = os.path.join(task_dir, filename)
    file.save(file_path)
    
    # إنشاء بيئة افتراضية للمهمة
    venv_path = os.path.join('venvs', task_id)
    process_manager.create_task(task_id)
    
    if not create_virtual_env(venv_path):
        return jsonify({'error': 'فشل إنشاء البيئة الافتراضية'}), 500
    
    process_manager.update_process(task_id, venv_path=venv_path)
    
    # استخراج المكتبات المطلوبة
    process_manager.add_output(task_id, '🔍 جاري تحليل الملف واستخراج المكتبات المطلوبة...')
    libraries = extract_imports_from_file(file_path)
    
    if libraries:
        process_manager.add_output(task_id, f'📚 المكتبات المكتشفة: {", ".join(libraries)}')
    else:
        process_manager.add_output(task_id, 'ℹ️ لم يتم اكتشاف مكتبات خارجية')
    
    # بدء التثبيت والتشغيل في Thread منفصل
    auto_run = request.form.get('auto_run', 'true') == 'true'
    
    thread = threading.Thread(target=process_and_run, 
                            args=(task_id, venv_path, file_path, libraries, auto_run))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'task_id': task_id,
        'message': 'تم رفع الملف بنجاح',
        'filename': filename,
        'libraries': libraries,
        'auto_run': auto_run
    })

def process_and_run(task_id, venv_path, file_path, libraries, auto_run):
    """معالجة الملف: تثبيت المكتبات ثم التشغيل"""
    try:
        # تثبيت المكتبات إذا وجدت
        if libraries:
            process_manager.add_output(task_id, '\n📥 بدء تثبيت المكتبات...')
            process_manager.add_output(task_id, '-'*40)
            successful, failed = install_libraries(venv_path, libraries, task_id)
            process_manager.add_output(task_id, '-'*40)
            
            if failed:
                process_manager.add_output(task_id, 
                    f'\n⚠️ تحذير: بعض المكتبات فشل تثبيتها. قد لا يعمل البوت بشكل صحيح.\n', True)
        
        # تشغيل البوت تلقائياً إذا كان الخيار مفعلاً
        if auto_run:
            run_bot(venv_path, file_path, task_id)
        else:
            process_manager.add_output(task_id, '\n✅ تم تثبيت المكتبات. اضغط على زر التشغيل لبدء البوت.')
            process_manager.update_process(task_id, completed=True, success=True)
            
    except Exception as e:
        process_manager.add_output(task_id, f'\n❌ حدث خطأ غير متوقع: {str(e)}', True)
        process_manager.update_process(task_id, completed=True, success=False)

@app.route('/run/<task_id>', methods=['POST'])
def run_task(task_id):
    """تشغيل البوت يدوياً"""
    status = process_manager.get_status(task_id)
    if not status:
        return jsonify({'error': 'المهمة غير موجودة'}), 404
    
    if status['running']:
        return jsonify({'error': 'البوت يعمل بالفعل'}), 400
    
    # البحث عن ملف .py في مجلد المهمة
    task_dir = os.path.join(app.config['UPLOAD_FOLDER'], task_id)
    py_files = list(Path(task_dir).glob('*.py'))
    
    if not py_files:
        return jsonify({'error': 'لم يتم العثور على ملف Python'}), 404
    
    file_path = str(py_files[0])
    venv_path = os.path.join('venvs', task_id)
    
    # إعادة تعيين حالة المهمة
    process_manager.create_task(task_id)
    process_manager.update_process(task_id, venv_path=venv_path)
    
    # تشغيل البوت
    thread = threading.Thread(target=run_bot, args=(venv_path, file_path, task_id))
    thread.daemon = True
    thread.start()
    
    return jsonify({'message': 'تم بدء تشغيل البوت', 'task_id': task_id})

@app.route('/stop/<task_id>', methods=['POST'])
def stop_task(task_id):
    """إيقاف البوت"""
    status = process_manager.get_status(task_id)
    if not status:
        return jsonify({'error': 'المهمة غير موجودة'}), 404
    
    if not status['running']:
        return jsonify({'error': 'البوت لا يعمل حالياً'}), 400
    
    process_manager.stop_task(task_id)
    
    return jsonify({'message': 'تم إيقاف البوت', 'task_id': task_id})

@app.route('/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """الحصول على حالة المهمة"""
    status = process_manager.get_status(task_id)
    if not status:
        return jsonify({'error': 'المهمة غير موجودة'}), 404
    return jsonify(status)

@app.route('/output/<task_id>')
def output_stream(task_id):
    """بث المخرجات مباشرة"""
    def generate():
        if task_id not in process_manager.outputs:
            yield f"data: {json.dumps({'error': 'المهمة غير موجودة'})}\n\n"
            return
        
        last_index = 0
        while True:
            status = process_manager.get_status(task_id)
            if not status:
                break
            
            # إرسال المخرجات الجديدة
            current_outputs = status['output_list']
            current_errors = status['error_list']
            
            # المخرجات العادية
            while last_index < len(current_outputs):
                data = {
                    'type': 'output',
                    'text': current_outputs[last_index],
                    'running': status['running'],
                    'completed': status['completed']
                }
                yield f"data: {json.dumps(data)}\n\n"
                last_index += 1
            
            # إرسال الأخطاء
            if status['error_list']:
                for error in status['error_list']:
                    data = {
                        'type': 'error',
                        'text': error,
                        'running': status['running'],
                        'completed': status['completed']
                    }
                    yield f"data: {json.dumps(data)}\n\n"
            
            # إرسال حالة الإكمال
            if status['completed']:
                data = {
                    'type': 'complete',
                    'success': status['success'],
                    'running': False,
                    'completed': True
                }
                yield f"data: {json.dumps(data)}\n\n"
                break
            
            time.sleep(0.5)
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/cleanup/<task_id>', methods=['POST'])
def cleanup(task_id):
    """تنظيف ملفات المهمة"""
    try:
        # إيقاف البوت إذا كان يعمل
        process_manager.stop_task(task_id)
        
        # حذف الملفات
        task_dir = os.path.join(app.config['UPLOAD_FOLDER'], task_id)
        venv_path = os.path.join('venvs', task_id)
        
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir)
        if os.path.exists(venv_path):
            shutil.rmtree(venv_path)
        
        # حذف من الذاكرة
        with process_manager.lock:
            process_manager.processes.pop(task_id, None)
            process_manager.outputs.pop(task_id, None)
        
        return jsonify({'message': 'تم تنظيف الملفات بنجاح'})
    except Exception as e:
        return jsonify({'error': f'خطأ في التنظيف: {str(e)}'}), 500

@app.route('/health')
def health():
    """فحص صحة الخادم"""
    return jsonify({
        'status': 'healthy',
        'active_tasks': len(process_manager.processes),
        'uptime': time.time()
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)