# routers/admin.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from services import rag_service

router = APIRouter(prefix="/admin", tags=["Admin"])

# --- 1. กำหนดหน้าตา "แบบฟอร์ม" สินค้า (Schema) ---
class ProductSchema(BaseModel):
    id: str
    name: str
    category: str
    price: float
    stock: int
    description: str
    image_url: Optional[str] = None

# --- 2. API รับข้อมูล (POST /admin/feed) ---
@router.post("/feed")
async def feed_knowledge(products: List[ProductSchema]):
    """
    API สำหรับ Admin เพื่อป้อนข้อมูลสินค้า (เป็น List)
    """
    try:
        # แปลง Pydantic Model เป็น Dict ปกติ เพื่อส่งให้ Service
        product_dicts = [p.model_dump() for p in products]
        
        # เรียกทีมสมองให้บันทึกข้อมูล
        await rag_service.add_knowledge(product_dicts)
        
        return {
            "status": "success", 
            "message": f"Imported {len(products)} items successfully"
        }
        
    except Exception as e:
        print(f"Error feeding data: {e}")
        raise HTTPException(status_code=500, detail=str(e))