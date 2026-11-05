from fastapi import FastAPI

from app.orders.api import router as orders_router

app = FastAPI(title="orders")
app.include_router(orders_router)


@app.get("/health")
def health():
    return {"ok": True}
