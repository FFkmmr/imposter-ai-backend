from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from routers import users, categories, ai, config, webhooks, admin

app = FastAPI(
    title="Imposter AI API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(users.router, prefix="/v1")
app.include_router(categories.router, prefix="/v1")
app.include_router(ai.router, prefix="/v1")
app.include_router(config.router, prefix="/v1")
app.include_router(webhooks.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": None}},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
