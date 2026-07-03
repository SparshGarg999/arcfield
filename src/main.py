"""FastAPI application entry point."""

from fastapi import FastAPI

app = FastAPI(
    title="Arcfield",
    description="Durable Game Economy Service",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "healthy"}
