"""Langfuse 5 / OpenTelemetry observations, created around real work.

The bounded SDK batch processor exports asynchronously. No cloud call or flush is
performed on the RAG request thread. A local exporter is used without credentials.
"""

import base64
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from urllib.parse import urlparse

from dotenv import load_dotenv
from langfuse import Langfuse
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

log = logging.getLogger("owasp.rag")
BLOCKED = "LANGFUSE CLOUD VERIFICATION: BLOCKED — USER CREDENTIALS REQUIRED"


def mask_text(value):
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", str(value))
    text = re.sub(
        r"(?i)(password|secret|api[_ -]?key|authorization|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text
    )
    text = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    return text


class Exporter(SpanExporter):
    def __init__(self, remote=None):
        self.remote, self.spans = remote, deque(maxlen=512)
        self.last_export, self.lock = "not_attempted", threading.Lock()
        self.accepted_trace_ids = deque(maxlen=512)

    def export(self, spans):
        with self.lock:
            self.spans.extend(spans)
        if self.remote:
            try:
                result = self.remote.export(spans)
                self.last_export = "accepted" if result == SpanExportResult.SUCCESS else "failed"
                if result == SpanExportResult.SUCCESS:
                    with self.lock:
                        self.accepted_trace_ids.extend(format(s.context.trace_id, "032x") for s in spans)
                return result
            except Exception:
                self.last_export = "failed"
                return SpanExportResult.FAILURE
        self.last_export = "local_only"
        return SpanExportResult.SUCCESS

    def shutdown(self):
        if self.remote:
            self.remote.shutdown()


class NullSpan:
    def update(self, **kwargs):
        return self

    def end(self):
        return self


class Observation:
    def __init__(self, trace, name, as_type="span", **fields):
        self.trace, self.name, self.started = trace, name, time.perf_counter()
        self.closed = False
        try:
            self.span = trace.root.start_observation(name=name, as_type=as_type, **fields)
        except Exception:
            self.span = NullSpan()
            trace.owner.instrumentation_errors += 1

    def update(self, **fields):
        try:
            self.span.update(**fields)
        except Exception:
            self.trace.owner.instrumentation_errors += 1
        return self

    def end(self, **metadata):
        if not self.closed:
            self.update(metadata=metadata)
            try:
                self.span.end()
            except Exception:
                self.trace.owner.instrumentation_errors += 1
            duration = round((time.perf_counter() - self.started) * 1000, 3)
            self.trace.stages.append({"name": self.name, "duration_ms": duration, **metadata})
            log.info(
                self.name,
                extra={
                    "operation": "rag",
                    "context": {
                        "request_id": self.trace.request_id,
                        "trace_id": self.trace.trace_id,
                        "duration_ms": duration,
                        **metadata,
                    },
                },
            )
            self.closed = True


class RequestTrace:
    def __init__(self, owner, request_id, metadata):
        self.owner, self.request_id, self.stages, self.open = owner, request_id, [], []
        self.trace_id = Langfuse.create_trace_id(seed=request_id)
        try:
            self.root = owner.client.start_observation(
                name="OWASP_RAG_REQUEST",
                trace_context={"trace_id": self.trace_id},
                metadata={"request_id": request_id, **metadata},
            )
        except Exception:
            self.root = NullSpan()
            owner.instrumentation_errors += 1

    def start(self, name, as_type="span", **fields):
        span = Observation(self, name, as_type, **fields)
        self.open.append(span)
        return span

    @contextmanager
    def step(self, name, as_type="span", **fields):
        span = self.start(name, as_type, **fields)
        try:
            yield span
        except BaseException as exc:
            span.update(level="ERROR", status_message=type(exc).__name__)
            span.end(status="error", error_type=type(exc).__name__)
            raise
        finally:
            span.end()

    def close(self, status):
        for span in self.open:
            if not span.closed:
                span.end(status="interrupted")
        try:
            self.root.update(metadata={"request_id": self.request_id, "status": status})
            self.root.end()
        except Exception:
            self.owner.instrumentation_errors += 1


class Tracing:
    def __init__(self, root, exporter=None, credentials=True):
        load_dotenv(root / ".env", override=False)
        public = os.getenv("LANGFUSE_PUBLIC_KEY", "") if credentials else ""
        secret = os.getenv("LANGFUSE_SECRET_KEY", "") if credentials else ""
        self.base_url = os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
        try:
            parsed = urlparse(self.base_url)
            valid = (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            valid = False
        self.configured = bool(public and secret and valid)
        self.capture_texts = os.getenv("LANGFUSE_CAPTURE_TEXTS", "false").lower() == "true"
        self.instrumentation_errors = 0
        self.auth_status = "not_checked" if self.configured else "credentials_required"
        if public and secret and not valid:
            self.auth_status = "invalid_base_url"
        remote = None
        if self.configured and exporter is None:
            auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
            try:
                remote = OTLPSpanExporter(
                    endpoint=self.base_url.rstrip("/") + "/api/public/otel/v1/traces",
                    headers={"Authorization": "Basic " + auth, "x-langfuse-ingestion-version": "4"},
                    timeout=2,
                )
            except Exception:
                self.instrumentation_errors += 1
                self.auth_status = "exporter_initialization_failed"
        self.exporter = exporter or Exporter(remote)
        # Unique local key avoids Langfuse's public-key singleton sharing test providers.
        self.provider = TracerProvider()
        try:
            self.client = Langfuse(
                public_key=public if self.configured else "pk-local-" + uuid.uuid4().hex,
                secret_key=secret or "local-export-only",
                base_url=self.base_url if valid else "https://cloud.langfuse.com",
                timeout=2,
                tracer_provider=self.provider,
                span_exporter=self.exporter,
                flush_at=32,
                flush_interval=1,
                environment="local",
                release="owasp-rag-0.3.0",
            )
        except Exception:
            self.client = None
            self.instrumentation_errors += 1
            self.auth_status = "sdk_initialization_failed"
        self._trace_pattern = None
        if self.configured and self.client and (remote is not None or exporter is not None):
            threading.Thread(target=self.check, daemon=True).start()

    def texts(self, **values):
        return {k: mask_text(v) for k, v in values.items()} if self.capture_texts else None

    def request(self, request_id, metadata):
        return RequestTrace(self, request_id, metadata)

    def status(self):
        return {
            "sdk": "langfuse 5.0.4 / OpenTelemetry",
            "configured": self.configured,
            "cloud_verification": self.auth_status,
            "last_export": getattr(self.exporter, "last_export", "test_exporter"),
            "capture_texts": self.capture_texts,
            "instrumentation_errors": self.instrumentation_errors,
            "delivery": "bounded in-memory SDK queue; no durable replay",
            "message": (
                "Invalid LANGFUSE_BASE_URL; local instrumentation only"
                if self.auth_status == "invalid_base_url"
                else BLOCKED
            )
            if not self.configured
            else "Cloud delivery requires auth-check and accepted export",
        }

    def trace_url(self, trace_id):
        # Do not suggest cloud presence when nothing was exported successfully.
        if self._trace_pattern and trace_id in getattr(self.exporter, "accepted_trace_ids", ()):
            return self._trace_pattern.replace("0" * 32, trace_id)
        return None

    def check(self):
        if not self.configured:
            return self.status()
        try:
            self.auth_status = "authenticated" if self.client.auth_check() else "failed"
            if self.auth_status == "authenticated":
                self._trace_pattern = self.client.get_trace_url(trace_id="0" * 32)
        except Exception:
            self.auth_status = "unavailable"
        return self.status()

    def flush(self):
        # CLI/tests/shutdown only, never in a RAG request.
        self.provider.force_flush(timeout_millis=5000)

    def shutdown(self):
        self.provider.shutdown()
