# main.py
from fastapi import FastAPI
from contextlib import asynccontextmanager
from routers import webhook, admin
from services import rag_service # Import rag_service

# --- 1. กำหนด Lifespan (สิ่งที่ทำตอนเปิดและปิด) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # [ตอนเปิด]
    print("🟢 System Starting... Checking for initial data...")
    await rag_service.initialize_database() # <--- เรียกฟังก์ชันโหลดไฟล์ที่นี่!
    
    yield # จุดที่ API ทำงานปกติ
    
    # [ตอนปิด] (ถ้ามีอะไรต้องทำก่อนปิด)
    print("🔴 System Shutting down...")

# --- 2. สร้างแอปพร้อม Lifespan ---
app = FastAPI(lifespan=lifespan)
    
app.include_router(webhook.router)
app.include_router(admin.router)

@app.get("/")
def read_root():
    return {"Hello": "RAG Chatbot API is running!"}