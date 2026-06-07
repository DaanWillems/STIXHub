import uvicorn
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI()
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run("src.__main__:app")
