import os
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import httpx

# ==========================================
# 1. DATABASE SETUP
# ==========================================
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./payments.db")
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    trx_id = Column(String, unique=True, index=True)
    amount = Column(Float)
    sender_shortcode = Column(String)
    is_used = Column(Boolean, default=False)
    shopify_order_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 2. APP CONFIGURATION
# ==========================================
app = FastAPI(title="Shopify Auto Payment Fetcher")

# Allow requests from your Shopify storefront
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "super_secret_macro_password_123")
SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "")  # e.g., your-store.myshopify.com
SHOPIFY_ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "") # shpat_xxxxx

class ShopifyVerifyRequest(BaseModel):
    shopify_order_id: str
    trx_id: str
    order_amount: float

# ==========================================
# 3. RECEIVE SMS / NOTIFICATION WEBHOOK
# ==========================================
@app.post("/api/webhooks/sms")
async def receive_sms(
    request: Request,
    db: Session = Depends(get_db),
    x_webhook_secret: str = Header(None)
):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await request.json()
        sender = data.get("sender", "").strip()
        message = data.get("text", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    valid_senders = ["3737", "8558"]
    if sender not in valid_senders:
        return {"status": "ignored", "reason": "Not an official payment shortcode"}

    amount_match = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", message, re.IGNORECASE)
    trx_match = re.search(r"(?:TID|Trx\.?\s*ID)[:\s\-]*([A-Za-z0-9]+)", message, re.IGNORECASE)

    if not amount_match or not trx_match:
        return {"status": "ignored", "reason": "Could not parse Amount or TRX ID"}

    amount = float(amount_match.group(1).replace(",", ""))
    trx_id = trx_match.group(1).strip()

    existing_trx = db.query(Transaction).filter(Transaction.trx_id == trx_id).first()
    if existing_trx:
        return {"status": "ignored", "reason": "Transaction already recorded"}

    new_trx = Transaction(
        trx_id=trx_id,
        amount=amount,
        sender_shortcode=sender
    )
    db.add(new_trx)
    db.commit()

    return {"status": "success", "trx_id": trx_id, "amount": amount}

# ==========================================
# 4. VERIFY & AUTO-APPROVE SHOPIFY ORDER
# ==========================================
@app.post("/api/shopify/verify-and-approve")
async def verify_shopify_order(payload: ShopifyVerifyRequest, db: Session = Depends(get_db)):
    # 1. Look up the Transaction ID
    trx = db.query(Transaction).filter(Transaction.trx_id == payload.trx_id.strip()).first()

    if not trx:
        raise HTTPException(
            status_code=404,
            detail="Transaction ID not found. Ensure your payment went through and wait 30 seconds."
        )

    if trx.is_used:
        raise HTTPException(
            status_code=400,
            detail="This Transaction ID has already been applied to an order."
        )

    # 2. Validate payment amount
    if trx.amount < payload.order_amount:
        raise HTTPException(
            status_code=400,
            detail=f"Incomplete payment. Expected Rs. {payload.order_amount}, but received Rs. {trx.amount}."
        )

    # 3. Call Shopify Admin API to mark the order as Paid
    shopify_api_url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/orders/{payload.shopify_order_id}/transactions.json"
    headers = {
        "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN,
        "Content-Type": "application/json"
    }
    body = {
        "transaction": {
            "kind": "capture",
            "status": "success",
            "amount": str(payload.order_amount)
        }
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(shopify_api_url, json=body, headers=headers)
        if res.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail=f"Failed to update Shopify order: {res.text}"
            )

    # 4. Mark transaction as consumed in DB
    trx.is_used = True
    trx.shopify_order_id = payload.shopify_order_id
    db.commit()

    return {"status": "success", "message": "Payment verified and order approved!"}
