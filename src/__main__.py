import argparse
import asyncio

import uvicorn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STIXHub")
    parser.add_argument(
        "--mode", choices=["server", "worker", "server-worker"], required=True
    )
    return parser.parse_args()


async def run_server_worker() -> None:
    from worker.__main__ import Worker

    config = uvicorn.Config("server.__main__:app")
    server = uvicorn.Server(config)
    worker = Worker()
    await asyncio.gather(server.serve(), worker.run())


def main() -> None:
    args = parse_args()

    if args.mode == "server":
        uvicorn.run("server.__main__:app")
    elif args.mode == "worker":
        from worker.__main__ import Worker

        asyncio.run(Worker().run())
    else:  # server-worker
        asyncio.run(run_server_worker())


if __name__ == "__main__":
    main()
