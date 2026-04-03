# services/rag_service.py
import config
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_postgres import PGVector
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from langchain_core.prompts import PromptTemplate
from langchain_ollama.chat_models import ChatOllama
from langchain_core.documents import Document
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory

import json
import os
import asyncio

# -----------------------------
# Database connection
# -----------------------------
try:
    db_port = int(config.DB_PORT)
except (ValueError, TypeError):
    db_port = 5434

CONNECTION_STRING = (
    f"postgresql+psycopg://{config.DB_USER}:{config.DB_PASSWORD}"
    f"@{config.DB_HOST}:{db_port}/{config.DB_NAME}"
)

COLLECTION_NAME = "chatbot_admin_knowledge"

# -----------------------------
# Embedding model
# -----------------------------
embeddings = HuggingFaceEmbeddings(model_name="BAAI/bge-m3")

# -----------------------------
# Vector store
# -----------------------------
vectorstore = PGVector(
    connection=CONNECTION_STRING,
    embeddings=embeddings,
    collection_name=COLLECTION_NAME,
    use_jsonb=True,
    engine_args={
        "connect_args": {
            "sslmode": "disable"
        },
        "pool_pre_ping": True
    }
)

# Reduce retrieval size for faster inference
retriever = vectorstore.as_retriever(search_kwargs={"k": 2})

# -----------------------------
# LLM
# -----------------------------
llm = ChatOllama(
    base_url=config.OLLAMA_BASE_URL,
    model=config.OLLAMA_MODEL,
    temperature=0.2,
)

# -----------------------------
# Prompt
# -----------------------------
template = """
คุณคือ "แอดมินร้านค้า" หน้าที่ของคุณคือการนำเสนอสินค้าและช่วยตอบคำถามลูกค้าอย่างตรงประเด็น

กฎสำคัญ:
1. ใช้ข้อมูลจาก "บริบทสินค้า" เท่านั้น ห้ามแต่งข้อมูลเองเด็ดขาด
2. ถ้าลูกค้าถามเรื่องสี ไซส์ สต็อกย่อย รายละเอียดเฉพาะ หรือคุณสมบัติใดๆ ที่ไม่มีอยู่ในบริบท
   ให้ตอบตรงๆ ว่า "ขออภัยค่ะ/ครับ ขณะนี้แอดมินยังไม่มีข้อมูลส่วนนั้น"
3. ถ้าลูกค้าพิมพ์ต่อเนื่องหลายข้อความ ให้ถือว่าเป็นคำถามเดียวกัน
4. ถ้าลูกค้ากำลังถามต่อจากสินค้าที่เพิ่งคุยกัน ให้ตอบต่อเนื่องจากบริบทเดิม
   ไม่ต้องกลับไปเปิดการขายใหม่ทั้งหมดทุกครั้ง
5. ถ้าลูกค้าระบุชื่อสินค้ามาแล้ว ให้โฟกัสตอบเฉพาะสินค้านั้นก่อน
6. ถ้าไม่มีสินค้าที่ตรงเป๊ะ แต่มีสินค้าที่ใกล้เคียงในบริบท ให้แนะนำตัวที่ใกล้เคียงที่สุด
7. ปฏิเสธเฉพาะเมื่อคำถามไม่เกี่ยวกับสินค้าในร้านเลย
8. ใช้ภาษาไทยสุภาพ เป็นธรรมชาติ กระชับ และอ่านง่าย
9. ถ้าคำถามเป็นข้อมูลเฉพาะ เช่น "มีไซส์ 20 ไหม" และบริบทไม่มีข้อมูลไซส์
   ห้ามตอบวนกลับไปโปรโมตสินค้าซ้ำ ให้ตอบว่าไม่มีข้อมูลไซส์โดยตรง

ประวัติการสนทนา:
{chat_history}

บริบทสินค้า:
{context}

คำถามลูกค้า:
{question}

คำตอบ:
"""

prompt = PromptTemplate.from_template(template)

# -----------------------------
# In-memory session history
# -----------------------------
session_store: dict[str, InMemoryChatMessageHistory] = {}


def get_session_history(session_id: str):
    if session_id not in session_store:
        session_store[session_id] = InMemoryChatMessageHistory()

    history = session_store[session_id]

    # Keep history shorter for faster responses
    MAX_MESSAGES = 6
    if len(history.messages) > MAX_MESSAGES:
        history.messages = history.messages[-MAX_MESSAGES:]

    return history


# -----------------------------
# Optional fast exact-name lookup
# -----------------------------
PRODUCT_CACHE: list[dict] = []


def normalize_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


def build_product_content(product: dict) -> str:
    sizes = product.get("sizes")
    colors = product.get("colors")

    sizes_text = ", ".join(map(str, sizes)) if sizes else "ไม่มีข้อมูล"
    colors_text = ", ".join(map(str, colors)) if colors else "ไม่มีข้อมูล"

    return f"""
ชื่อสินค้า: {product['name']}
หมวดหมู่: {product['category']}
รายละเอียด: {product['description']}
ราคา: {product['price']} บาท
สต็อก: {product['stock']}
สี: {colors_text}
ไซส์: {sizes_text}
""".strip()


def find_exact_product_matches(query_text: str) -> list[dict]:
    q = normalize_text(query_text)
    matches = []

    for p in PRODUCT_CACHE:
        name = normalize_text(p.get("name", ""))
        if name and name in q:
            matches.append(p)

    return matches


def build_context_from_products(products: list[dict]) -> str:
    chunks = [build_product_content(p) for p in products]
    return "\n\n---\n\n".join(chunks)


# -----------------------------
# RAG chain
# -----------------------------
rag_chain = (
    RunnablePassthrough.assign(
        context=(lambda x: retriever.invoke(x["question"]))
    )
    | prompt
    | llm
    | StrOutputParser()
)

chain_with_history = RunnableWithMessageHistory(
    rag_chain,
    get_session_history,
    input_messages_key="question",
    history_messages_key="chat_history",
)

print("✅ RAG Service: Initialized Successfully!")
print(f"✅ Connected to Vector Store: {COLLECTION_NAME}")
print(f"✅ LLM Model: {config.OLLAMA_MODEL}")


# -----------------------------
# Main response function
# -----------------------------
async def get_rag_response(query_text: str, sender_id: str = "default_user") -> str:
    print(f"RAG Service: Received query '{query_text}' from '{sender_id}'")

    try:
        # Fast path: if exact product name appears in the query,
        # build direct context from cache to reduce vector retrieval overhead
        exact_matches = find_exact_product_matches(query_text)

        if exact_matches:
            direct_context = build_context_from_products(exact_matches)

            direct_chain = (
                prompt
                | llm
                | StrOutputParser()
            )

            response = await asyncio.to_thread(
                direct_chain.invoke,
                {
                    "chat_history": get_session_history(sender_id).messages,
                    "context": direct_context,
                    "question": query_text,
                }
            )

            # Manually update history for fast path
            history = get_session_history(sender_id)
            history.add_user_message(query_text)
            history.add_ai_message(response)

            print(f"RAG Service: Generated fast-path response '{response}'")
            return response

        # Default RAG path
        response = await asyncio.to_thread(
            chain_with_history.invoke,
            {"question": query_text},
            {"configurable": {"session_id": sender_id}}
        )

        print(f"RAG Service: Generated response '{response}'")
        return response

    except Exception as e:
        print(f"Error invoking RAG chain: {e}")
        return "ขออภัยค่ะ ระบบกำลังมีปัญหา กรุณาลองใหม่อีกครั้ง"


# -----------------------------
# Add knowledge
# -----------------------------
async def add_knowledge(products: list[dict]):
    documents = []

    print(f"RAG Service: Processing {len(products)} items...")

    for p in products:
        content = build_product_content(p)

        meta = {
            "product_id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "price": p["price"],
            "stock": p["stock"],
            "image_url": p.get("image_url", "-"),
            "sizes": p.get("sizes", []),
            "colors": p.get("colors", []),
        }

        doc = Document(page_content=content, metadata=meta)
        documents.append(doc)

    if documents:
        await asyncio.to_thread(vectorstore.add_documents, documents)

    # Refresh in-memory product cache
    global PRODUCT_CACHE
    PRODUCT_CACHE = products.copy()

    print(f"✅ RAG Service: Added {len(documents)} documents to knowledge base.")
    return True


# -----------------------------
# Initial load
# -----------------------------
async def initialize_database():
    """
    Load products from JSON on startup.
    """
    candidate_paths = [
        "data/products.json",
        "products.json",
    ]

    file_path = None
    for p in candidate_paths:
        if os.path.exists(p):
            file_path = p
            break

    if not file_path:
        print("⚠️ Startup: products.json not found. Skipping auto-load.")
        return

    print(f"🚀 Startup: Found '{file_path}'. Loading data...")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            products = json.load(f)

        if products:
            await add_knowledge(products)
            print("✅ Startup: Data loaded successfully!")
        else:
            print("⚠️ Startup: JSON file is empty.")

    except Exception as e:
        print(f"❌ Startup Error: {e}")