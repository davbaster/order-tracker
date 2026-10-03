import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
import logging


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

resource = Resource.create({"service.name": "order-tracker"})
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(), export_interval_millis=5000
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
lookup_requests = metrics.get_meter(__name__).create_counter(
    "order_lookup_requests",
    description="Number of order lookup HTTP requests",
    unit="{request}",
)

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
set_logger_provider(logger_provider)
lookup_logger = logging.getLogger("order_tracker.lookups")
lookup_logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))
lookup_logger.setLevel(logging.INFO)
lookup_logger.propagate = False


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at.replace(day=placed_at.day + 2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for status_code in range(500, 600):
        lookup_requests.add(
            0,
            {
                "http.route": "/api/orders/{order_id}",
                "http.response.status_code": status_code,
            },
        )
    init_db()
    yield


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.middleware("http")
async def record_order_lookup_request(request: Request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        route = getattr(request.scope.get("route"), "path", None)
        if route == "/api/orders/{order_id}":
            lookup_requests.add(
                1,
                {"http.route": route, "http.response.status_code": 500},
            )
        raise
    route = getattr(request.scope.get("route"), "path", None)
    if route == "/api/orders/{order_id}":
        lookup_requests.add(
            1,
            {"http.route": route, "http.response.status_code": response.status_code},
        )
    return response


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with tracer.start_as_current_span("order.lookup") as span:
        span.set_attribute("http.route", "/api/orders/{order_id}")
        span.set_attribute("order.id", order_id)
        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            status_code = 404
            order = None
        else:
            try:
                order = order_detail(row)
                status_code = 200
            except Exception:
                status_code = 500
                span.set_status(trace.Status(trace.StatusCode.ERROR, "Order lookup failed"))
                span.set_attribute("http.response.status_code", status_code)
                lookup_logger.exception(
                    "Order lookup failed",
                    extra={
                        "http.route": "/api/orders/{order_id}",
                        "http.response.status_code": status_code,
                        "order.id": order_id,
                        "order.found": True,
                    },
                )
                raise
        span.set_attribute("http.response.status_code", status_code)
        lookup_logger.info(
            "Order lookup completed",
            extra={
                "http.route": "/api/orders/{order_id}",
                "http.response.status_code": status_code,
                "order.id": order_id,
                "order.found": row is not None,
            },
        )
    if row is None:
        raise HTTPException(404, "Order not found")
    return order


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
