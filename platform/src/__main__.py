import contextlib
from collections.abc import AsyncGenerator
from database import db

import uvicorn
from fastapi import FastAPI

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    await db.create_tables()
    yield
    await db.dispose()

def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("src.__main__:app")
