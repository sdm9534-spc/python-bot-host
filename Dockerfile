FROM python:3.11-slim

# تثبيت الأدوات الأساسية
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    make \
    libffi-dev \
    libssl-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نسخ الملفات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# إنشاء المجلدات
RUN mkdir -p uploads temp venvs

# تشغيل التطبيق مع gunicorn
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "0", "app:app"]