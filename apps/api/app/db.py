from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, url: str):
        self.url = url
        engine_kwargs: dict[str, object] = {"future": True}

        if url.startswith("sqlite"):
            if url.endswith(":memory:") or "mode=memory" in url:
                engine_kwargs.update(
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool,
                )
            else:
                engine_kwargs.update(connect_args={"check_same_thread": False})
                self._ensure_sqlite_parent(url)
        else:
            engine_kwargs["pool_pre_ping"] = True

        self.engine = create_engine(url, **engine_kwargs)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            return
        raw_path = url[len(prefix) :].split("?", 1)[0]
        if not raw_path or raw_path == ":memory:":
            return
        Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def create_schema(self) -> None:
        from . import models  # noqa: F401 - imports model metadata

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
        finally:
            db.close()
