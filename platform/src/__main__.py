import contextlib
from collections.abc import AsyncGenerator
from database import db
from routes.taxii2 import taxii2_router

import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    await db.create_tables()
    yield
    await db.dispose()


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(taxii2_router)
    return app


app = create_app()


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("src.__main__:app")
