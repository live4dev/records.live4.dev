#!/usr/bin/env python3
"""Archive records_live4dev media, captions, posters, and audio for the static site."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "records.json"
STATE_FILE = ROOT / "data" / "import-state.json"
CONTENT_DIR = ROOT / "content"
DEFAULT_CHANNELS = "records_live4dev"
VIDEO_SUFFIXES = {".m4v", ".mkv", ".mov", ".mp4", ".webm", ".avi"}
AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
SUPPORTED_SUFFIXES = VIDEO_SUFFIXES | AUDIO_SUFFIXES
LOGGER = logging.getLogger("telegram-import")


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session: str
    channels: tuple[str, ...]
    history_limit: int | None
    max_file_bytes: int | None
    dry_run: bool
    full_history: bool = False
    refresh: bool = False

    @classmethod
    def from_environment(cls, args: argparse.Namespace) -> "Config":
        channel_value = args.channels or os.getenv("TELEGRAM_CHANNELS", DEFAULT_CHANNELS)
        channels = tuple(dict.fromkeys(
            normalized for item in channel_value.split(",")
            if (normalized := normalize_channel(item))
        ))
        if channels != (DEFAULT_CHANNELS,):
            raise ValueError("This archive imports only the records_live4dev channel")
        history_limit = nonnegative_int(
            os.getenv("TELEGRAM_HISTORY_LIMIT", "0"), "TELEGRAM_HISTORY_LIMIT"
        ) or None
        max_file_mb = nonnegative_int(
            os.getenv("TELEGRAM_MAX_FILE_MB", "0"), "TELEGRAM_MAX_FILE_MB"
        )
        return cls(
            api_id=positive_int(required_env("TELEGRAM_API_ID"), "TELEGRAM_API_ID"),
            api_hash=required_env("TELEGRAM_API_HASH"),
            session=required_env("TELEGRAM_SESSION"),
            channels=channels,
            history_limit=history_limit,
            max_file_bytes=max_file_mb * 1024 * 1024 if max_file_mb else None,
            dry_run=args.dry_run,
            full_history=getattr(args, "full_history", False),
            refresh=getattr(args, "refresh", False),
        )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def nonnegative_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if result < 0:
        raise ValueError(f"{name} must be zero (unlimited) or greater than zero")
    return result


def positive_int(value: str, name: str) -> int:
    result = nonnegative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def normalize_channel(value: str) -> str:
    candidate = value.strip().removeprefix("https://t.me/").removeprefix("@")
    candidate = candidate.rstrip("/,").lower()
    if candidate and not re.fullmatch(r"[a-z0-9_]{4,64}", candidate):
        raise ValueError(f"Invalid Telegram channel: {value!r}")
    return candidate


def read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read {path.name}: {error}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} must contain a JSON object")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_value) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def document_metadata(message: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for attribute in getattr(getattr(message, "document", None), "attributes", []) or []:
        for key in ("duration", "title", "performer", "file_name"):
            value = getattr(attribute, key, None)
            if value not in (None, ""):
                metadata[key] = value
    return metadata


def media_kind(message: Any) -> str | None:
    file = getattr(message, "file", None)
    mime_type = str(getattr(file, "mime_type", "") or "").lower()
    suffix = Path(str(getattr(file, "name", "") or "")).suffix.lower()
    if getattr(message, "video", None) or mime_type.startswith("video/") or suffix in VIDEO_SUFFIXES:
        return "video"
    if (getattr(message, "audio", None) or getattr(message, "voice", None)
            or mime_type.startswith("audio/") or suffix in AUDIO_SUFFIXES):
        return "audio"
    return None


def clean_text(value: Any, limit: int = 140) -> str:
    """Shorten display titles only; captions never pass through this function."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def caption_text(message: Any) -> str:
    return str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")


def album_captions(messages: list[Any]) -> dict[int, Any]:
    """Use an album's caption for its other media, preserving the caption source."""
    captions: dict[int, Any] = {}
    for message in sorted(messages, key=lambda item: int(item.id)):
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is not None and caption_text(message).strip():
            captions.setdefault(int(grouped_id), message)
    return captions


def extract_tags(caption: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in re.findall(r"(?<![\w#])#(\w+)", caption, flags=re.UNICODE):
        if tag.casefold() not in seen:
            tags.append(tag)
            seen.add(tag.casefold())
    return tags


def record_title(message: Any, metadata: dict[str, Any], kind: str, caption: str | None = None) -> str:
    raw_caption = caption_text(message) if caption is None else caption
    lines = [line.strip() for line in raw_caption.splitlines() if line.strip()]
    if lines:
        return clean_text(lines[0], 140)
    if metadata.get("title"):
        performer = clean_text(metadata.get("performer"), 80)
        title = clean_text(metadata["title"], 120)
        return f"{performer} — {title}" if performer else title
    file_name = metadata.get("file_name") or getattr(getattr(message, "file", None), "name", "")
    if file_name:
        return clean_text(Path(str(file_name)).stem.replace("_", " "), 140)
    return "Видеозапись" if kind == "video" else "Аудиозапись"


def media_suffix(message: Any, kind: str, metadata: dict[str, Any]) -> str:
    file = getattr(message, "file", None)
    suffix = Path(str(metadata.get("file_name") or getattr(file, "name", "") or "")).suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return suffix
    guessed = mimetypes.guess_extension(str(getattr(file, "mime_type", "") or "").lower())
    if guessed in SUPPORTED_SUFFIXES:
        return str(guessed)
    if getattr(message, "voice", None):
        return ".ogg"
    return ".mp4" if kind == "video" else ".mp3"


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_record(message: Any, channel: str, kind: str, metadata: dict[str, Any],
                 relative_file: str, caption_source: Any | None = None) -> dict[str, Any]:
    file = getattr(message, "file", None)
    source = caption_source if caption_source is not None else message
    caption = caption_text(source)
    published = getattr(message, "date", None) or datetime.now(timezone.utc)
    record = {
        "id": f"{channel}:{message.id}", "channel": channel,
        "message_id": int(message.id), "kind": kind,
        "title": record_title(message, metadata, kind, caption),
        "description": caption, "caption_original": caption_text(message),
        "tags": extract_tags(caption), "published_at": iso_utc(published),
        "duration_seconds": float(metadata.get("duration") or 0),
        "file": relative_file,
        "mime_type": str(getattr(file, "mime_type", "") or mimetypes.guess_type(relative_file)[0] or ""),
        "size_bytes": int(getattr(file, "size", 0) or 0),
        "telegram_url": f"https://t.me/{channel}/{message.id}",
        "original_filename": str(metadata.get("file_name") or getattr(file, "name", "") or ""),
        "expected_size_bytes": int(getattr(file, "size", 0) or 0),
        "telegram_mime_type": str(getattr(file, "mime_type", "") or ""),
        "metadata_file": Path(relative_file).with_suffix(".json").as_posix(),
        "caption_file": Path(relative_file).with_suffix(".txt").as_posix(),
        "telegram_document_id": getattr(getattr(message, "document", None), "id", None),
        "grouped_id": getattr(message, "grouped_id", None),
        "caption_source_message_id": int(source.id),
    }
    if getattr(message, "edit_date", None):
        record["edited_at"] = iso_utc(message.edit_date)
    if kind == "audio":
        record["audio_origin"] = "original"
    return record


def run_media_command(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RuntimeError(f"Cannot run {arguments[0]}: {error}") from error
    if result.returncode:
        raise RuntimeError(f"{Path(arguments[0]).name} failed: {result.stderr[-2400:].strip()}")
    return result.stdout


def probe_media(path: Path, kind: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty {kind} file: {path.name}")
    output = run_media_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ])
    try:
        probe = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ffprobe returned invalid JSON for {path.name}") from error
    if not any(stream.get("codec_type") == kind for stream in probe.get("streams", [])):
        raise RuntimeError(f"No {kind} stream in {path.name}")
    return probe


def probe_duration(probe: dict[str, Any], fallback: float = 0) -> float:
    candidates = [probe.get("format", {}).get("duration")]
    candidates.extend(stream.get("duration") for stream in probe.get("streams", []))
    for value in candidates:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    return fallback


def safe_catalog_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = (ROOT / value).resolve()
    return candidate if candidate.is_relative_to(ROOT.resolve()) else None


def file_identity_matches(record: dict[str, Any] | None, message: Any) -> bool:
    old_id = (record or {}).get("telegram_document_id")
    new_id = getattr(getattr(message, "document", None), "id", None)
    return old_id is None or new_id is None or str(old_id) == str(new_id)


async def download_original(client: Any, message: Any, target: Path,
                            old_record: dict[str, Any] | None = None) -> None:
    """Resume MTProto downloads from a .part file and publish only complete media."""
    expected = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    target.parent.mkdir(parents=True, exist_ok=True)
    unchanged = file_identity_matches(old_record, message)
    if target.is_file() and unchanged and target.stat().st_size > 0:
        if not expected or target.stat().st_size == expected:
            return
    legacy = safe_catalog_path((old_record or {}).get("file"))
    if unchanged and legacy and legacy != target and legacy.is_file() and legacy.stat().st_size > 0:
        if not expected or legacy.stat().st_size == expected:
            shutil.copy2(legacy, target)
            return
    partial = target.with_suffix(target.suffix + ".part")
    identity_file = partial.with_suffix(partial.suffix + ".json")
    identity = {"document_id": getattr(getattr(message, "document", None), "id", None), "size_bytes": expected}
    if partial.exists():
        previous_identity = read_json(identity_file, {})
        if previous_identity != identity or (expected and partial.stat().st_size > expected):
            partial.unlink()
    write_json_atomic(identity_file, identity)
    offset = partial.stat().st_size if partial.exists() else 0
    LOGGER.info("Downloading message %s -> %s (resume at %s bytes)", message.id, target.name, offset)
    if not expected or offset < expected:
        with partial.open("ab") as output:
            async for chunk in client.iter_download(
                message.media, offset=offset, request_size=512 * 1024, file_size=expected or None,
            ):
                output.write(chunk)
    downloaded_size = partial.stat().st_size if partial.exists() else 0
    if downloaded_size == 0 or (expected and downloaded_size != expected):
        raise RuntimeError(f"Incomplete download for message {message.id}: {downloaded_size}/{expected} bytes")
    partial.replace(target)
    identity_file.unlink(missing_ok=True)


def create_poster(video: Path, target: Path, duration: float, regenerate: bool = False) -> None:
    if not regenerate and target.is_file() and target.stat().st_size > 0:
        try:
            probe_media(target, "video")
            return
        except RuntimeError:
            pass
    temporary = target.with_suffix(".part.jpg")
    seek = min(1.0, duration / 3) if duration else 0
    run_media_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", str(seek),
        "-i", str(video), "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", str(temporary),
    ])
    probe_media(temporary, "video")
    temporary.replace(target)


def extract_audio(video: Path, video_probe: dict[str, Any], output_stem: Path,
                  regenerate: bool = False) -> tuple[Path, dict[str, Any]]:
    streams = [stream for stream in video_probe.get("streams", []) if stream.get("codec_type") == "audio"]
    if not streams:
        raise RuntimeError(f"Video {video.name} has no audio stream to extract")
    stream = streams[0]
    copy_aac = stream.get("codec_name") == "aac"
    source_duration = probe_duration({"streams": [stream]}, probe_duration(video_probe))
    target = output_stem.with_suffix(".m4a" if copy_aac else ".mp3")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not regenerate and target.is_file() and target.stat().st_size > 0:
        try:
            probe = probe_media(target, "audio")
            audio_duration = probe_duration(probe)
            expected_codec = "aac" if copy_aac else "mp3"
            if not any(item.get("codec_type") == "audio" and item.get("codec_name") == expected_codec
                       for item in probe.get("streams", [])):
                raise RuntimeError(f"Incorrect codec in extracted audio {target.name}")
            if source_duration and abs(source_duration - audio_duration) > max(1.5, source_duration * 0.01):
                raise RuntimeError(f"Extracted audio duration differs from {video.name}")
            return target, probe
        except RuntimeError:
            pass
    temporary = target.with_suffix(target.suffix + ".part")
    codec_arguments = (["-c:a", "copy", "-movflags", "+faststart", "-f", "ipod"] if copy_aac else
                       ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3", "-f", "mp3"])
    run_media_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(video),
        "-map", f"0:{stream['index']}", "-vn", *codec_arguments, str(temporary),
    ])
    probe = probe_media(temporary, "audio")
    audio_duration = probe_duration(probe)
    if source_duration and abs(source_duration - audio_duration) > max(1.5, source_duration * 0.01):
        raise RuntimeError(f"Incomplete extracted audio for {video.name}: {audio_duration}/{source_duration} seconds")
    temporary.replace(target)
    return target, probe


def raw_metadata(message: Any, record: dict[str, Any], caption_source: Any) -> dict[str, Any]:
    payload = {
        "record": record, "caption_original": record["caption_original"],
        "caption_effective": record["description"],
        "message_caption_original": caption_text(message),
        "caption_source_message_id": int(caption_source.id),
        "entities": getattr(message, "entities", None) or [],
        "caption_entities": getattr(caption_source, "entities", None) or [],
        "date": getattr(message, "date", None), "edit_date": getattr(message, "edit_date", None),
        "grouped_id": getattr(message, "grouped_id", None),
        "reply_to": getattr(message, "reply_to", None),
        "forwarded_from": getattr(message, "fwd_from", None),
        "document_attributes": getattr(getattr(message, "document", None), "attributes", None) or [],
    }
    if hasattr(message, "to_dict"):
        payload["telegram_message"] = message.to_dict()
    return payload


def write_sidecars(target: Path, message: Any, record: dict[str, Any], caption_source: Any) -> None:
    write_json_atomic(target.with_suffix(".json"), raw_metadata(message, record, caption_source))
    text_path = target.with_suffix(".txt")
    temporary = text_path.with_suffix(".txt.tmp")
    temporary.write_text(record["description"], encoding="utf-8")
    temporary.replace(text_path)


async def download_record(client: Any, message: Any, channel: str, kind: str,
                          metadata: dict[str, Any], dry_run: bool,
                          caption_source: Any | None = None,
                          old_record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = caption_source if caption_source is not None else message
    target = CONTENT_DIR / kind / f"{message.id}{media_suffix(message, kind, metadata)}"
    existing_file = safe_catalog_path((old_record or {}).get("file"))
    if (existing_file and existing_file.is_file() and file_identity_matches(old_record, message)
            and existing_file.parent == CONTENT_DIR / kind):
        target = existing_file
    record = build_record(message, channel, kind, metadata, target.relative_to(ROOT).as_posix(), source)
    if dry_run:
        LOGGER.info("Would archive @%s/%s, caption, tags%s", channel, message.id,
                    ", poster and extracted audio" if kind == "video" else "")
        return [record]
    unchanged = file_identity_matches(old_record, message)
    await download_original(client, message, target, old_record)
    try:
        probe = await asyncio.to_thread(probe_media, target, kind)
    except RuntimeError:
        # An existing file with a matching byte count can still be corrupt. Keep
        # the bad copy for diagnostics and attempt a fresh Telegram download.
        target.replace(target.with_suffix(target.suffix + ".corrupt"))
        LOGGER.warning("Invalid existing media for message %s; downloading it again", message.id)
        await download_original(client, message, target)
        probe = await asyncio.to_thread(probe_media, target, kind)
    record["duration_seconds"] = probe_duration(probe, record["duration_seconds"])
    record["size_bytes"] = target.stat().st_size
    record["mime_type"] = mimetypes.guess_type(target.name)[0] or record["mime_type"]
    record["availability"] = "local"
    records = [record]
    if kind == "video":
        poster = target.with_suffix(".jpg")
        await asyncio.to_thread(create_poster, target, poster, record["duration_seconds"], not unchanged)
        audio_stem = CONTENT_DIR / "audio" / f"{message.id}-video"
        existing_audio = safe_catalog_path((old_record or {}).get("audio_file"))
        if existing_audio and existing_audio.parent == CONTENT_DIR / "audio" and unchanged:
            audio_stem = existing_audio.with_suffix("")
        audio, audio_probe = await asyncio.to_thread(extract_audio, target, probe, audio_stem, not unchanged)
        record["poster"] = poster.relative_to(ROOT).as_posix()
        record["audio_file"] = audio.relative_to(ROOT).as_posix()
        derivative = dict(record)
        derivative.update({
            "id": record["id"] + ":audio", "kind": "audio", "audio_origin": "extracted",
            "source_video_id": record["id"], "file": record["audio_file"],
            "source_original_filename": record["original_filename"],
            "metadata_file": audio.with_suffix(".json").relative_to(ROOT).as_posix(),
            "caption_file": audio.with_suffix(".txt").relative_to(ROOT).as_posix(),
            "mime_type": "audio/mp4" if audio.suffix == ".m4a" else "audio/mpeg",
            "size_bytes": audio.stat().st_size,
            "duration_seconds": probe_duration(audio_probe, record["duration_seconds"]),
            "audio_codec": next(stream.get("codec_name", "") for stream in audio_probe["streams"]
                                if stream.get("codec_type") == "audio"),
        })
        derivative.pop("audio_file", None)
        derivative.pop("original_filename", None)
        records.append(derivative)
        write_sidecars(audio, message, derivative, source)
    write_sidecars(target, message, record, source)
    return records


def upsert_records(items: list[dict[str, Any]], records: list[dict[str, Any]]) -> int:
    indexes = {str(item.get("id")): index for index, item in enumerate(items)}
    added = 0
    for record in records:
        index = indexes.get(record["id"])
        if index is None:
            indexes[record["id"]] = len(items)
            items.append(record)
            added += 1
        else:
            items[index] = record
    return added


def needs_repair(record: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> bool:
    file = safe_catalog_path(record.get("file"))
    expected_prefix = f"content/{record.get('kind')}/"
    if not str(record.get("file", "")).startswith(expected_prefix):
        return True
    required = [file, file.with_suffix(".json") if file else None, file.with_suffix(".txt") if file else None]
    if record.get("kind") == "video":
        required.extend([safe_catalog_path(record.get("poster")), safe_catalog_path(record.get("audio_file"))])
        derivative = items_by_id.get(str(record.get("id")) + ":audio")
        if not derivative or needs_repair(derivative, items_by_id):
            return True
    return any(path is None or not path.is_file()
               or (path.suffix != ".txt" and path.stat().st_size == 0) for path in required)


def update_catalog_summary(catalog: dict[str, Any], items: list[dict[str, Any]],
                           channel_state: dict[str, Any], now: str | None = None) -> dict[str, Any]:
    """Keep progress counts accurate when continuing a previously partial archive."""
    now = now or iso_utc(datetime.now(timezone.utc))
    records = [item for item in items if item.get("channel") == DEFAULT_CHANNELS]
    originals = [item for item in records if not item.get("source_video_id")]
    videos = [item for item in originals if item.get("kind") == "video"]
    audio = [item for item in originals if item.get("kind") == "audio"]
    by_id = {str(item.get("id")): item for item in records}

    def local(item: dict[str, Any]) -> bool:
        path = safe_catalog_path(item.get("file"))
        return bool(path and path.is_file() and path.stat().st_size > 0)

    missing = [item for item in originals if not local(item)]
    pending = {int(item["message_id"]) for item in originals if needs_repair(item, by_id)}
    if channel_state.get("failed_message_id"):
        pending.add(int(channel_state["failed_message_id"]))
    source = catalog.setdefault("source", {})
    if not isinstance(source, dict):
        source = catalog["source"] = {}
    source.update({"channel": DEFAULT_CHANNELS, "url": f"https://t.me/{DEFAULT_CHANNELS}"})
    for key in ("history_complete", "scanned_messages", "latest_message_id", "oldest_message_id"):
        if key in channel_state:
            source[key] = channel_state[key]
    summary = catalog.setdefault("import", {})
    if not isinstance(summary, dict):
        summary = catalog["import"] = {}
    summary.update({
        "status": "partial" if pending or not source.get("history_complete") else "complete",
        "history_complete": bool(source.get("history_complete")),
        "original_video_count": len(videos), "original_audio_count": len(audio),
        "downloaded_original_video_count": sum(local(item) for item in videos),
        "downloaded_original_audio_count": sum(local(item) for item in audio),
        "extracted_audio_count": sum(local(item) for item in records if item.get("source_video_id")),
        "pending_original_count": len(missing), "pending_message_ids": sorted(pending),
        "pending_work_count": len(pending),
        "total_original_bytes": sum(int(item.get("expected_size_bytes") or item.get("size_bytes") or 0)
                                    for item in originals),
        "downloaded_original_bytes": sum(safe_catalog_path(item["file"]).stat().st_size
                                         for item in originals if local(item)),
        "downloaded_at": now,
    })
    for key in ("scanned_messages", "latest_message_id", "oldest_message_id"):
        if key in source:
            summary[key] = source[key]
    summary.pop("blocked_reason", None)
    summary.pop("blocker_detail", None)
    if channel_state.get("last_error"):
        summary.update({"blocked_reason": "import_failed", "blocker_detail": channel_state["last_error"]})
    catalog["updated_at"] = now
    catalog["items"] = items
    return summary


async def import_channel(client: Any, config: Config, channel: str,
                         items: list[dict[str, Any]], channel_state: dict[str, Any],
                         checkpoint: Any | None = None) -> tuple[int, bool]:
    """Process oldest first; a media failure never moves the checkpoint past it."""
    last_message_id = int(channel_state.get("last_message_id", 0) or 0)
    full_scan = (config.full_history or config.refresh or not channel_state.get("initialized")
                 or (config.history_limit is None and not channel_state.get("archive_complete")))
    min_id = 0 if full_scan else last_message_id
    limit = None if (config.full_history or config.refresh or not full_scan) else config.history_limit
    LOGGER.info("Reading @%s (%s)", channel,
                "full history" if full_scan and limit is None else
                f"latest {limit} messages" if full_scan else f"after message {last_message_id}")
    entity = await client.get_entity(channel)
    messages = [message async for message in client.iter_messages(entity, min_id=min_id, limit=limit)]
    messages.sort(key=lambda message: int(message.id))
    items_by_id = {str(item.get("id")): item for item in items}
    if not full_scan:
        repair_ids = sorted({int(item["message_id"]) for item in items
                             if item.get("channel") == channel and not item.get("source_video_id")
                             and needs_repair(item, items_by_id)})
        if repair_ids:
            repaired = await client.get_messages(entity, ids=repair_ids)
            for message_id, message in zip(repair_ids, repaired):
                if message is None:
                    raise RuntimeError(f"Cannot repair deleted/unavailable message @{channel}/{message_id}")
            messages = sorted({int(message.id): message for message in [*messages, *repaired]}.values(),
                              key=lambda message: int(message.id))
    # An incremental batch can begin in the middle of an album. Fetch its siblings.
    present_ids = {int(message.id) for message in messages}
    for message in list(messages):
        if getattr(message, "grouped_id", None) is not None:
            sibling_ids = [index for index in range(max(1, int(message.id) - 10), int(message.id) + 11)
                           if index not in present_ids]
            if sibling_ids:
                siblings = await client.get_messages(entity, ids=sibling_ids)
                for sibling in siblings:
                    if sibling is not None and getattr(sibling, "grouped_id", None) == message.grouped_id:
                        messages.append(sibling)
                        present_ids.add(int(sibling.id))
    messages.sort(key=lambda message: int(message.id))
    captions = album_captions(messages)
    added = 0
    changed = False
    if full_scan and not config.dry_run:
        channel_state["archive_complete"] = False
        channel_state["last_message_id"] = 0
        channel_state["history_complete"] = limit is None
        channel_state["scanned_messages"] = len(messages)
        if messages:
            channel_state["latest_message_id"] = int(messages[-1].id)
            channel_state["oldest_message_id"] = int(messages[0].id)
    for message in messages:
        kind = media_kind(message)
        if kind:
            try:
                size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
                if config.max_file_bytes is not None and size > config.max_file_bytes:
                    raise RuntimeError(f"{size / 1024 / 1024:.1f} MB exceeds TELEGRAM_MAX_FILE_MB; "
                                       "increase the cap or set it to 0 to resume")
                source = message
                if not caption_text(message).strip() and getattr(message, "grouped_id", None) in captions:
                    source = captions[message.grouped_id]
                records = await download_record(
                    client, message, channel, kind, document_metadata(message), config.dry_run,
                    source, items_by_id.get(f"{channel}:{message.id}"),
                )
                added += upsert_records(items, records)
                items_by_id.update({record["id"]: record for record in records})
            except Exception as error:
                if not config.dry_run:
                    channel_state["archive_complete"] = False
                    channel_state["status"] = "partial"
                    channel_state["last_message_id"] = min(
                        int(channel_state.get("last_message_id", 0)), int(message.id) - 1,
                    )
                    channel_state["failed_message_id"] = int(message.id)
                    channel_state["last_error"] = str(error)
                    if checkpoint:
                        checkpoint()
                raise RuntimeError(f"Failed @{channel}/{message.id}: {error}; rerun to resume") from error
        if not config.dry_run:
            channel_state["last_message_id"] = max(int(channel_state.get("last_message_id", 0)), int(message.id))
            channel_state.pop("failed_message_id", None)
            channel_state.pop("last_error", None)
            if kind:
                channel_state["pending_message_ids"] = [
                    message_id for message_id in channel_state.get("pending_message_ids", [])
                    if int(message_id) != int(message.id)
                ]
        changed = True
        if checkpoint and not config.dry_run:
            checkpoint()
    if not config.dry_run:
        channel_state["initialized"] = True
        final_items_by_id = {str(record.get("id")): record for record in items}
        pending = sorted({int(item["message_id"]) for item in items
                          if item.get("channel") == channel and not item.get("source_video_id")
                          and needs_repair(item, final_items_by_id)})
        channel_state["pending_message_ids"] = pending
        channel_state["status"] = "partial" if pending else "complete"
        if full_scan:
            channel_state["archive_complete"] = limit is None and not pending
        if checkpoint:
            checkpoint()
    LOGGER.info("@%s: %s new catalog record(s), %s messages examined", channel, added, len(messages))
    return added, changed


async def run(config: Config) -> int:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as error:
        raise RuntimeError("Install dependencies with: pip install -r requirements.txt") from error
    if not config.dry_run:
        for executable in ("ffmpeg", "ffprobe"):
            if not shutil.which(executable):
                raise RuntimeError(f"{executable} is required; install FFmpeg before importing")
        for kind in ("video", "audio"):
            (CONTENT_DIR / kind).mkdir(parents=True, exist_ok=True)
    catalog = read_json(DATA_FILE, {"updated_at": None, "items": []})
    raw_items = catalog.get("items", [])
    if not isinstance(raw_items, list):
        raise RuntimeError("data/records.json: items must be an array")
    items: list[dict[str, Any]] = [item for item in raw_items if isinstance(item, dict)]
    import_state = read_json(STATE_FILE, {"channels": {}, "updated_at": None})
    channels_state = import_state.setdefault("channels", {})
    if not isinstance(channels_state, dict):
        raise RuntimeError("data/import-state.json: channels must be an object")
    manifest_path = CONTENT_DIR / "archive-manifest.json"
    manifest = read_json(manifest_path, {}) if manifest_path.exists() else None

    def checkpoint() -> None:
        now = iso_utc(datetime.now(timezone.utc))
        items.sort(key=lambda item: str(item.get("published_at", "")), reverse=True)
        summary = update_catalog_summary(catalog, items, channels_state.get(DEFAULT_CHANNELS, {}), now)
        write_json_atomic(DATA_FILE, catalog)
        import_state["updated_at"] = now
        write_json_atomic(STATE_FILE, import_state)
        if manifest is not None:
            manifest.update({"updated_at": now, "source": catalog["source"], "import": summary})
            write_json_atomic(manifest_path, manifest)

    client = TelegramClient(StringSession(config.session), config.api_id, config.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("TELEGRAM_SESSION is not authorized; generate a new session")
        total_added = 0
        for channel in config.channels:
            channel_state = channels_state.setdefault(channel, {})
            if not isinstance(channel_state, dict):
                raise RuntimeError(f"Invalid import state for {channel}")
            added, _ = await import_channel(client, config, channel, items, channel_state, checkpoint)
            total_added += added
    finally:
        await client.disconnect()
    LOGGER.info("%s complete: %s record(s) %s", "Dry run" if config.dry_run else "Import",
                total_added, "would be added" if config.dry_run else "added")
    return total_added


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", help="Source channel (records_live4dev); overrides TELEGRAM_CHANNELS")
    parser.add_argument("--dry-run", action="store_true", help="Read Telegram without downloading or writing files")
    parser.add_argument("--full-history", action="store_true", help="Revisit complete history, ignoring history limit/checkpoint")
    parser.add_argument("--refresh", action="store_true", help="Revisit complete history to update edits and repair assets")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run(Config.from_environment(parse_args())))
    except (ValueError, RuntimeError, OSError) as error:
        LOGGER.error("%s", error)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
