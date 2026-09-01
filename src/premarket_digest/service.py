"""Leaf orchestration: source gates, render, outbox, and isolated delivery."""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from src.alerts.discord import (
    DiscordDeliveryError,
    DiscordNotifier,
    is_discord_snowflake,
)
from src.utils.io import atomic_save_json

from .groups import GroupArtifactDigestSource
from .models import DigestChannel, ScheduleSkip, SourceGateError
from .momentum import CompletedSessionMomentumSource
from .render import (
    build_momentum_payload,
    build_sector_rotation_payload,
    payload_markdown,
)
from .schedule import resolve_premarket_context
from .settings import PremarketDigestSettings
from .state import ConcurrentDigestWorkerError, DigestStateStore, payload_hash


class PremarketDigestService:
    def __init__(
        self,
        settings: PremarketDigestSettings,
        *,
        state_store: DigestStateStore | None = None,
        momentum_source: Any | None = None,
        group_source: Any | None = None,
        notifier_factory: Callable[[str], Any] = DiscordNotifier,
        now: Callable[[], datetime] | None = None,
        calendar: Any | None = None,
    ) -> None:
        self.settings = settings
        self._state_store = state_store
        self._momentum_source = momentum_source
        self._group_source = group_source
        self.notifier_factory = notifier_factory
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.calendar = calendar

    @property
    def state_store(self) -> DigestStateStore:
        # A dry-run must not create or mutate the delivery ledger.
        if self._state_store is None:
            self._state_store = DigestStateStore(self.settings.state_path)
        return self._state_store

    def _feature_enabled(self, channel: DigestChannel) -> bool:
        return (
            self.settings.momentum_enabled
            if channel is DigestChannel.MOMENTUM
            else self.settings.sector_rotation_enabled
        )

    def _scheduled_send_allowed(
        self,
        context: Any,
        *,
        scheduled: bool,
        delay_seconds: float = 0.0,
    ) -> bool:
        if not scheduled:
            return True
        candidate_now = self.now()
        if candidate_now.tzinfo is None:
            candidate_now = candidate_now.replace(tzinfo=timezone.utc)
        candidate_now = candidate_now + timedelta(seconds=max(0.0, delay_seconds))
        try:
            current = resolve_premarket_context(
                self.settings,
                now=candidate_now,
                scheduled=True,
                calendar=self.calendar,
            )
        except ScheduleSkip:
            return False
        return current.target_session == context.target_session

    def _build(self, channel: DigestChannel, context: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if channel is DigestChannel.MOMENTUM:
            source = self._momentum_source or CompletedSessionMomentumSource(self.settings)
            report = source.load(context.source_session)
            payload = build_momentum_payload(report, context, self.settings)
            metadata = {
                "candidate_count": report.get("candidate_count"),
                "exact_asof_coverage": report.get("exact_asof_coverage"),
                "evaluable_history_coverage": report.get(
                    "evaluable_history_coverage"
                ),
                "universe_manifest_source_session": report.get(
                    "universe_manifest_source_session"
                ),
                "universe_manifest_refreshed_at": report.get(
                    "universe_manifest_refreshed_at"
                ),
                "input_fingerprint": report.get("input_fingerprint"),
            }
        else:
            source = self._group_source or GroupArtifactDigestSource(
                self.settings,
                now=context.now_utc,
            )
            report = source.load(context.source_session)
            payload = build_sector_rotation_payload(report, context, self.settings)
            metadata = {
                "partial": report.get("partial", False),
                "available_levels": sorted((report.get("levels") or {}).keys()),
                "level_errors": report.get("errors") or {},
                "run_ids": {
                    level: value.get("run_id")
                    for level, value in (report.get("levels") or {}).items()
                },
            }
        return payload, metadata

    def _deliver_claim(
        self,
        channel: DigestChannel,
        context: Any,
        claim: Any,
        webhook_url: str,
        *,
        scheduled: bool,
    ) -> dict[str, Any]:
        if claim.action == "already_sent":
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "SKIPPED_ALREADY_SENT",
                "message_id": claim.message_id,
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        if claim.action == "unknown_blocked":
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "UNKNOWN",
                "error_code": "DELIVERY_RESULT_UNKNOWN",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        if claim.action == "permanent_blocked":
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "FAILED_PERMANENT",
                "error_code": claim.error_code or "PERMANENT_FAILURE_FROZEN",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        try:
            allowed = self._scheduled_send_allowed(context, scheduled=scheduled)
        except Exception:
            self.state_store.release_unsent(
                context.target_session,
                channel,
                reason="The XNYS delivery-window recheck failed before the HTTP request.",
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "FAILED_RETRYABLE",
                "error_code": "SCHEDULE_RECHECK_FAILED",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        if not allowed:
            self.state_store.release_unsent(
                context.target_session,
                channel,
                reason="The scheduled premarket window closed before the HTTP request.",
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "SKIPPED_OUTSIDE_WINDOW",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        try:
            notifier = self.notifier_factory(webhook_url)
            if scheduled:
                try:
                    request_timeout = max(0.0, float(getattr(notifier, "timeout", 15.0)))
                except (TypeError, ValueError):
                    request_timeout = 15.0
                # requests' scalar timeout applies independently to connect
                # and read phases, so reserve both plus a small scheduling margin.
                completion_margin = (request_timeout * 2.0) + 2.0
                try:
                    completion_allowed = self._scheduled_send_allowed(
                        context,
                        scheduled=True,
                        delay_seconds=completion_margin,
                    )
                except Exception:
                    raise DiscordDeliveryError(
                        "XNYS delivery-window validation failed before the request",
                        uncertain=False,
                        retryable=True,
                        reason="schedule_recheck_failed",
                    ) from None
                if not completion_allowed:
                    raise DiscordDeliveryError(
                        "Discord delivery cannot complete before the premarket deadline",
                        uncertain=False,
                        retryable=False,
                        reason="delivery_window_closed",
                    )
                if hasattr(notifier, "request_guard"):
                    def request_guard(delay: float) -> bool:
                        try:
                            return self._scheduled_send_allowed(
                                context,
                                scheduled=True,
                                delay_seconds=max(0.0, delay) + completion_margin,
                            )
                        except Exception:
                            raise DiscordDeliveryError(
                                "XNYS delivery-window validation failed before the request",
                                uncertain=False,
                                retryable=True,
                                reason="schedule_recheck_failed",
                            ) from None

                    notifier.request_guard = request_guard
            result = notifier.send(claim.payload)
        except DiscordDeliveryError as exc:
            if exc.reason == "delivery_window_closed":
                self.state_store.release_unsent(
                    context.target_session,
                    channel,
                    reason="The Discord rate-limit delay crossed the premarket deadline.",
                )
                return {
                    "channel": channel.value,
                    "destination": channel.destination,
                    "status": "SKIPPED_OUTSIDE_WINDOW",
                    "attempts": claim.attempts,
                    "payload_hash": claim.payload_hash,
                }
            self.state_store.mark_failed(
                context.target_session,
                channel,
                error_code=str(exc.reason).upper(),
                error_message="Discord delivery did not produce a confirmed message ID.",
                uncertain=exc.uncertain,
                retryable=exc.retryable,
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": (
                    "UNKNOWN"
                    if exc.uncertain
                    else ("FAILED_RETRYABLE" if exc.retryable else "FAILED_PERMANENT")
                ),
                "error_code": str(exc.reason).upper(),
                "http_status": exc.status_code,
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        except ValueError:
            self.state_store.mark_failed(
                context.target_session,
                channel,
                error_code="INVALID_WEBHOOK_CONFIGURATION",
                error_message="The channel webhook configuration is invalid.",
                uncertain=False,
                retryable=False,
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "FAILED_PERMANENT",
                "error_code": "INVALID_WEBHOOK_CONFIGURATION",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        except Exception:  # never serialize a third-party exception or URL
            self.state_store.mark_failed(
                context.target_session,
                channel,
                error_code="DELIVERY_ADAPTER_ERROR",
                error_message="The delivery adapter failed before confirmation.",
                uncertain=True,
                retryable=False,
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "UNKNOWN",
                "error_code": "DELIVERY_ADAPTER_ERROR",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        message_value = result.get("message_id") if isinstance(result, dict) else None
        if message_value is None or not str(message_value).strip():
            self.state_store.mark_failed(
                context.target_session,
                channel,
                error_code="MISSING_MESSAGE_ID",
                error_message="The delivery adapter returned no confirmed message ID.",
                uncertain=True,
                retryable=False,
            )
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "UNKNOWN",
                "error_code": "MISSING_MESSAGE_ID",
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        message_id = str(message_value)
        try:
            self.state_store.mark_sent(
                context.target_session,
                channel,
                message_id,
                source_session=context.source_session,
                payload=claim.payload,
                expected_payload_hash=claim.payload_hash,
                attempts=claim.attempts,
            )
        except Exception:
            # Discord is confirmed, but the local commit is not. Never ask
            # systemd to retry a request that could duplicate the message.
            return {
                "channel": channel.value,
                "destination": channel.destination,
                "status": "UNKNOWN",
                "error_code": "STATE_COMMIT_AFTER_CONFIRMED_DELIVERY",
                "message_id": message_id,
                "attempts": claim.attempts,
                "payload_hash": claim.payload_hash,
            }
        return {
            "channel": channel.value,
            "destination": channel.destination,
            "status": "SENT",
            "http_status": result.get("status"),
            "message_id": message_id,
            "attempts": claim.attempts,
            "payload_hash": claim.payload_hash,
        }

    def _process_channel(
        self,
        channel: DigestChannel,
        context: Any,
        *,
        send: bool,
        prepare: bool,
        retry_unknown: bool,
        scheduled: bool,
        rebuild_failed: bool,
    ) -> dict[str, Any]:
        base = {"channel": channel.value, "destination": channel.destination}
        if not self._feature_enabled(channel):
            return {**base, "status": "SKIPPED_DISABLED"}
        if send and not self._scheduled_send_allowed(context, scheduled=scheduled):
            return {**base, "status": "SKIPPED_OUTSIDE_WINDOW"}

        if prepare:
            existing = self.state_store.get(context.target_session, channel)
            if existing is not None:
                existing_source = str(existing.get("source_session") or "")
                if existing_source != context.source_session:
                    return {
                        **base,
                        "status": "FAILED_PERMANENT",
                        "error_code": "PREPARED_SOURCE_SESSION_MISMATCH",
                    }
                return {
                    **base,
                    "status": "PREPARED_ALREADY_EXISTS",
                    "delivery_status": str(existing.get("status") or ""),
                    "payload_hash": existing.get("payload_hash"),
                }

        webhook_url = self.settings.webhook_for(channel)
        if send:
            existing = self.state_store.get(context.target_session, channel)
            frozen_status = str((existing or {}).get("status") or "")
            if existing is not None and (
                frozen_status == "SENT"
                or (frozen_status in {"SENDING", "UNKNOWN"} and not retry_unknown)
            ):
                claim = self.state_store.claim(
                    context.target_session,
                    channel,
                    retry_unknown=False,
                )
                return self._deliver_claim(
                    channel,
                    context,
                    claim,
                    webhook_url,
                    scheduled=scheduled,
                )
            if not webhook_url:
                return {
                    **base,
                    "status": "FAILED_PERMANENT",
                    "error_code": "WEBHOOK_NOT_CONFIGURED",
                }
            if existing is not None and not (
                rebuild_failed and frozen_status == "FAILED"
            ):
                claim = self.state_store.claim(
                    context.target_session,
                    channel,
                    retry_unknown=retry_unknown,
                )
                return self._deliver_claim(
                    channel,
                    context,
                    claim,
                    webhook_url,
                    scheduled=scheduled,
                )
            if scheduled and existing is None:
                return {
                    **base,
                    "status": "FAILED_RETRYABLE",
                    "error_code": "PREPARED_PAYLOAD_MISSING",
                }

        role_id = self.settings.role_for(channel)
        if role_id and not is_discord_snowflake(role_id):
            return {
                **base,
                "status": "FAILED_PERMANENT",
                "error_code": "INVALID_ROLE_ID",
            }

        try:
            payload, metadata = self._build(channel, context)
        except SourceGateError as exc:
            return {
                **base,
                "status": "FAILED_RETRYABLE",
                "error_code": exc.code,
                "details": exc.details,
            }
        except DiscordDeliveryError as exc:
            return {
                **base,
                "status": "FAILED_PERMANENT",
                "error_code": str(exc.reason).upper(),
            }
        except Exception as exc:  # details stay deliberately generic and secret-free
            return {
                **base,
                "status": "FAILED_RETRYABLE",
                "error_code": "SOURCE_ADAPTER_ERROR",
                "error_type": type(exc).__name__,
            }
        digest = payload_hash(payload)
        if prepare:
            staged = self.state_store.stage(
                context.target_session,
                channel,
                context.source_session,
                payload,
            )
            return {
                **base,
                "status": "PREPARED",
                "delivery_status": str(staged.get("status") or ""),
                "payload_hash": digest,
                "metadata": metadata,
            }
        if not send:
            return {
                **base,
                "status": "DRY_RUN",
                "payload_hash": digest,
                "payload": payload,
                "preview_markdown": payload_markdown(payload),
                "metadata": metadata,
            }

        if not self._scheduled_send_allowed(context, scheduled=scheduled):
            return {**base, "status": "SKIPPED_OUTSIDE_WINDOW", "metadata": metadata}

        self.state_store.stage(
            context.target_session,
            channel,
            context.source_session,
            payload,
            rebuild_failed=rebuild_failed,
        )
        claim = self.state_store.claim(
            context.target_session,
            channel,
            retry_unknown=retry_unknown,
        )
        result = self._deliver_claim(
            channel,
            context,
            claim,
            webhook_url,
            scheduled=scheduled,
        )
        result["metadata"] = metadata
        return result

    @staticmethod
    def _exit_code(results: list[dict[str, Any]]) -> int:
        statuses = {str(result.get("status") or "") for result in results}
        # A safe-to-retry channel must get its systemd retry even when the
        # other channel is permanently broken or uncertain. On the next run,
        # SENT/UNKNOWN channels are skipped by their independent outbox rows.
        if "FAILED_RETRYABLE" in statuses:
            return 1
        if "UNKNOWN" in statuses:
            return 3
        if "FAILED_PERMANENT" in statuses:
            return 2
        return 0

    def run(
        self,
        *,
        send: bool = False,
        prepare: bool = False,
        scheduled: bool = False,
        requested_session: str | None = None,
        channels: Iterable[DigestChannel] | None = None,
        retry_unknown: bool = False,
        rebuild_failed: bool = False,
        write_preview: bool = True,
    ) -> dict[str, Any]:
        selected = list(channels or list(DigestChannel))
        recovery_requested = retry_unknown or rebuild_failed
        if prepare and (send or scheduled or recovery_requested):
            return {
                "mode": "prepare",
                "status": "FAILED_CONFIGURATION",
                "error_code": "INVALID_PREPARE_SCOPE",
                "exit_code": 2,
                "channels": [channel.value for channel in selected],
            }
        if recovery_requested and (
            not send
            or scheduled
            or not requested_session
            or len(selected) != 1
            or (retry_unknown and rebuild_failed)
        ):
            return {
                "mode": "send" if send else "dry-run",
                "status": "FAILED_CONFIGURATION",
                "error_code": "INVALID_RECOVERY_SCOPE",
                "exit_code": 2,
                "channels": [channel.value for channel in selected],
            }
        try:
            context = resolve_premarket_context(
                self.settings,
                now=self.now(),
                requested_session=requested_session,
                scheduled=scheduled,
                calendar=self.calendar,
            )
        except ScheduleSkip as exc:
            return {
                "mode": "send" if send else "dry-run",
                "status": exc.code,
                "message": str(exc),
                "exit_code": 0,
                "channels": [channel.value for channel in selected],
            }
        except Exception as exc:
            return {
                "mode": "send" if send else "dry-run",
                "status": "FAILED_CONFIGURATION",
                "error_code": "SCHEDULE_RESOLUTION_FAILED",
                "error_type": type(exc).__name__,
                "exit_code": 2,
                "channels": [channel.value for channel in selected],
            }
        if (send or prepare) and not self.settings.enabled:
            return {
                "mode": "send" if send else "prepare",
                "status": "FAILED_CONFIGURATION",
                "error_code": "PREMARKET_DIGEST_DISABLED",
                "target_session": context.target_session,
                "source_session": context.source_session,
                "exit_code": 2,
                "channels": [channel.value for channel in selected],
            }

        lock = self.state_store.run_lock() if send or prepare else nullcontext()
        try:
            with lock:
                results = []
                for channel in selected:
                    try:
                        result = self._process_channel(
                            channel,
                            context,
                            send=send,
                            prepare=prepare,
                            retry_unknown=retry_unknown,
                            scheduled=scheduled,
                            rebuild_failed=rebuild_failed,
                        )
                    except Exception as exc:
                        result = {
                            "channel": channel.value,
                            "destination": channel.destination,
                            "status": "FAILED_RETRYABLE",
                            "error_code": "CHANNEL_OR_STATE_ERROR",
                            "error_type": type(exc).__name__,
                        }
                    results.append(result)
        except ConcurrentDigestWorkerError:
            results = [
                {
                    "channel": channel.value,
                    "destination": channel.destination,
                    "status": "FAILED_RETRYABLE",
                    "error_code": "CONCURRENT_WORKER",
                }
                for channel in selected
            ]
        summary = {
            "mode": "send" if send else "prepare" if prepare else "dry-run",
            "status": "COMPLETED" if self._exit_code(results) == 0 else "PARTIAL_OR_FAILED",
            "target_session": context.target_session,
            "source_session": context.source_session,
            "generated_at": context.generated_at,
            "results": results,
            "exit_code": self._exit_code(results),
        }
        if not send and not prepare and write_preview:
            summary["preview_files"] = self._write_preview(summary)
        return summary

    def _write_preview(self, summary: dict[str, Any]) -> list[str]:
        stamp = str(summary["generated_at"]).replace(":", "").replace("Z", "Z")
        root = self.settings.dry_runs_dir / str(summary["target_session"])
        root.mkdir(parents=True, exist_ok=True)
        json_path = root / f"{stamp}.json"
        atomic_save_json(summary, json_path)
        paths = [str(json_path)]
        for result in summary.get("results") or []:
            markdown = result.get("preview_markdown")
            if not markdown:
                continue
            channel = str(result.get("channel") or "digest").replace("/", "_")
            path = root / f"{stamp}-{channel}.md"
            path.write_text(str(markdown), encoding="utf-8")
            paths.append(str(path))
        return paths


__all__ = ["PremarketDigestService"]
