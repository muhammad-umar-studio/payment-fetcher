import os
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Depends, Header
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ==========================================
# 1. DATABASE SETUP
# ==========================================
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./payments.db")

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

# CRITICAL ADDITIONS: Define SessionLocal and Base
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    trx_id = Column(String, unique=True, index=True) # Prevent duplicate TRX IDs
    amount = Column(Float)
    sender_shortcode = Column(String)
    is_used = Column(Boolean, default=False) # True when assigned to an order
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 2. FASTAPI APP SETUP
# ==========================================
app = FastAPI(title="Custom Payment Gateway")

# SECURITY: Change this to a hard-to-guess password!
WEBHOOK_SECRET = "super_secret_macro_password_123"

# Request models
class VerifyPaymentRequest(BaseModel):
    trx_id: str
    expected_amount: float

# ==========================================
# 3. RECEIVE SMS WEBHOOK (FROM MACRODROID)
# ==========================================
@app.post("/api/webhooks/sms")
async def receive_sms(
    request: Request, 
    db: Session = Depends(get_db),
    x_webhook_secret: str = Header(None)
):
    # 1. Security Check: Ensure the request is actually from your phone
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        data = await request.json()
        sender = data.get("sender", "").strip()
        message = data.get("text", "").strip()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    print(f"DEBUG INCOMING WEBHOOK: Sender: {sender} | Message: {message}")

    # 2. Only allow official shortcodes
    valid_senders = ["3737", "8558"]
    if sender not in valid_senders:
        return {"status": "ignored", "reason": "Not an official payment shortcode"}

    # 3. Robust Regex for JazzCash and EasyPaisa
    amount_match = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", message, re.IGNORECASE)
    trx_match = re.search(r"(?:TID|Trx\.?\s*ID)[:\s\-]*([A-Za-z0-9]+)", message, re.IGNORECASE)

    if not amount_match or not trx_match:
        return {"status": "ignored", "reason": "Could not parse Amount or TRX ID"}

    amount = float(amount_match.group(1).replace(",", ""))
    trx_id = trx_match.group(1).strip()

    # 4. Save to Database (Ignore if TRX ID already exists)
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

    print(f"✅ Logged Payment: {amount} PKR, TRX: {trx_id}")
    return {"status": "success", "trx_id": trx_id, "amount": amount}

# ==========================================
# 4. VERIFY PAYMENT (FOR YOUR FRONTEND)
# ==========================================
@app.post("/api/verify-checkout")
async def verify_checkout(req: VerifyPaymentRequest, db: Session = Depends(get_db)):
    """
    Your React/Next.js frontend calls this when a user clicks 'Confirm Order'.
    It checks if the provided TRX ID exists and matches the cart total.
    """
    trx = db.query(Transaction).filter(Transaction.trx_id == req.trx_id).first()

    if not trx:
        raise HTTPException(status_code=404, detail="Transaction ID not found. Please wait a minute or check your spelling.")
    
    if trx.is_used:
        raise HTTPException(status_code=400, detail="This Transaction ID has already been used for another order.")

    if trx.amount < req.expected_amount:
        raise HTTPException(status_code=400, detail=f"Insufficient payment. Expected {req.expected_amount}, but received {trx.amount}.")

    # Mark as used so they can't use the same payment twice
    trx.is_used = True
    db.commit()

    return {"status": "success", "message": "Payment verified successfully!"}
