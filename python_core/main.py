import sqlite3
import datetime
import time
import json
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from passlib.context import CryptContext
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
import jwt

app = FastAPI()

# --- КОНФИГУРАЦИЯ ---
S3_ENDPOINT = "http://31.129.100.207:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
BUCKET_NAME = "globus-tasks"
SECRET_KEY = "supersecretkey"
ALGORITHM = "HS256"
DB_PATH = "../database/globus.db"
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Клиент S3 ---
s3_client = boto3.client('s3',
                         endpoint_url=S3_ENDPOINT,
                         aws_access_key_id=S3_ACCESS_KEY,
                         aws_secret_access_key=S3_SECRET_KEY,
                         config=Config(signature_version='s3v4'))

# --- БД ИНИЦИАЛИЗАЦИЯ ---
def init_db():
    # 1. БД SQLite
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Включаем WAL для конкурентного доступа Python и Go
    c.execute('PRAGMA journal_mode=WAL;')
    
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, role TEXT, full_name TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks 
                 (id INTEGER PRIMARY KEY, teacher_id INTEGER, student_id INTEGER, 
                  description TEXT, created_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS materials
                 (id INTEGER PRIMARY KEY, uploader_id INTEGER, title TEXT,
                  file_key TEXT, created_at TIMESTAMP)''')
    # Создаем админа по умолчанию, если нет
    try:
        hashed = pwd_context.hash("admin")
        c.execute("INSERT INTO users (username, password, role, full_name) VALUES (?, ?, ?, ?)", 
                  ("admin", hashed, "admin", "Главный администратор"))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

    # 2. Создание бакета в MinIO
    try:
        s3_client.create_bucket(Bucket=BUCKET_NAME)
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except Exception as e:
        print(f"Error connecting to MinIO: {e}")

init_db()

# --- МОДЕЛИ ---
class UserCreate(BaseModel):
    username: str
    password: str
    role: str # student, teacher, admin
    full_name: str

class TaskIn(BaseModel):
    student_id: int
    description: str

# --- AUTH UTILS ---
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload # {'sub': username, 'role': role, 'id': id}
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid credentials")

# --- ENDPOINTS ---

@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    user = c.execute("SELECT id, username, password, role, full_name FROM users WHERE username=?", (form_data.username,)).fetchone()
    conn.close()
    
    if not user or not pwd_context.verify(form_data.password, user[2]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    
    token_data = {"sub": user[1], "role": user[3], "id": user[0], "name": user[4]}
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": token, "token_type": "bearer", "role": user[3], "user_id": user[0], "full_name": user[4]}

# Администратор получает список пользователей
@app.get("/admin/users")
def list_users(current_user: dict = Depends(get_current_user)):
    if current_user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT id, username, role, full_name FROM users WHERE role != 'admin'").fetchall()
    conn.close()
    return [dict(u) for u in users]

# Общий эндпоинт для списка пользователей
@app.get("/common/users")
def public_list_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    users = conn.execute("SELECT id, username, role, full_name FROM users WHERE role != 'admin'").fetchall()
    conn.close()
    return [dict(u) for u in users]

# Администратор создает пользователей
@app.post("/admin/create_user")
def create_user(new_user: UserCreate, current_user: dict = Depends(get_current_user)):
    if current_user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Only admin can create users")
    
    hashed_pw = pwd_context.hash(new_user.password)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO users (username, password, role, full_name) VALUES (?, ?, ?, ?)", 
                  (new_user.username, hashed_pw, new_user.role, new_user.full_name))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="User already exists")
    return {"status": "User created"}

@app.delete("/admin/users/{user_id}")
def delete_user(user_id: int, current_user: dict = Depends(get_current_user)):
    if current_user['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "User deleted"}

# Учитель получает список студентов
@app.get("/teacher/students")
def list_students(current_user: dict = Depends(get_current_user)):
    if current_user['role'] != 'teacher':
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    students = conn.execute("SELECT id, username, full_name FROM users WHERE role='student'").fetchall()
    conn.close()
    return [dict(s) for s in students]

# Учитель выкладывает материал
@app.post("/teacher/materials")
def upload_material(
    title: str = Form(...),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user['role'] != 'teacher':
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    
    file_key = f"material_{int(time.time())}_{file.filename}"

    try:
        file.file.seek(0)
        s3_client.upload_fileobj(
            file.file, BUCKET_NAME, file_key,
            ExtraArgs={'ContentType': file.content_type}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO materials (uploader_id, title, file_key, created_at) VALUES (?, ?, ?, ?)",
              (current_user['id'], title, file_key, datetime.datetime.now()))
    conn.commit()
    conn.close()
    return {"status": "Материал загружен успешно"}

@app.get("/common/materials")
def get_materials():
    # Материалы видны всем
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('''SELECT 
                               m.*,
                               u.full_name AS uploader_name
                           FROM materials m
                           LEFT JOIN users u ON m.uploader_id = u.id
                           ORDER BY m.created_at DESC''').fetchall()
    conn.close()
    
    result = []
    for row in rows:
        item = dict(row)
        # Генерируем ссылку
        try:
            url = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': BUCKET_NAME, 'Key': item['file_key']},
                ExpiresIn=900
            )
            item['file_url'] = url
        except:
            item['file_url'] = None
        result.append(item)
    return result

# Учитель выкладывает задание
@app.post("/teacher/task")
async def create_task(
    student_id: int = Form(...),
    description: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user['role'] != 'teacher':
        raise HTTPException(status_code=403, detail="Only teachers can assign tasks")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (teacher_id, student_id, description, created_at) VALUES (?, ?, ?, ?)",
              (current_user['id'], student_id, description, datetime.datetime.now()))
    conn.commit()
    conn.close()
    return {"status": "Задание выдано успешно"}

# Ученик смотрит свои задания
@app.get("/student/tasks")
def get_tasks(current_user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Если учитель - видит созданные им, если ученик - видит свои
    if current_user['role'] == 'student':
        rows = c.execute("SELECT * FROM tasks WHERE student_id=?", (current_user['id'],)).fetchall()
    else:
        rows = c.execute("SELECT * FROM tasks WHERE teacher_id=?", (current_user['id'],)).fetchall()
    conn.close()

    return [dict(r) for r in rows]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8082)
