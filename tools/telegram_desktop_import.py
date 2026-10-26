#!/usr/bin/env python3
"""Complete the local records archive from a Telegram Desktop JSON export.

Run: python3 tools/telegram_desktop_import.py /path/to/export/result.json
The default moves verified originals. --copy retains a second media archive.
Only the previously inventoried channel and exact original byte sizes are accepted.
No Telegram account credentials or network access are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram_import import (
    ROOT, create_poster, extract_audio, extract_tags, needs_repair, probe_duration, probe_media,
    read_json, safe_catalog_path, upsert_records, write_json_atomic,
)

CHANNEL = "records_live4dev"
CHANNEL_NAME = "records.live4.dev"
CHANNEL_ID = 1443930717
CATALOG = ROOT / "data/records.json"
STATE = ROOT / "data/import-state.json"
MANIFEST = ROOT / "content/archive-manifest.json"
SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def channel_id(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    if str(numeric).startswith("-100"):
        return int(str(numeric)[4:])
    return abs(numeric)


def select_channel(payload: dict[str, Any]) -> dict[str, Any]:
    chats = payload.get("chats", {})
    candidates = chats.get("list", []) if isinstance(chats, dict) else []
    if isinstance(payload.get("messages"), list):
        candidates = [payload]
    matching = [chat for chat in candidates if channel_id(chat.get("id")) == CHANNEL_ID]
    if len(matching) != 1:
        raise ValueError(f"Export must contain exactly the inventoried channel @{CHANNEL} (id {CHANNEL_ID})")
    chat = matching[0]
    if chat.get("name") != CHANNEL_NAME:
        raise ValueError(f"Export channel name must be exactly {CHANNEL_NAME!r}; got {chat.get('name')!r}")
    username = chat.get("username") or chat.get("public_username")
    if username and str(username).removeprefix("@") != CHANNEL:
        raise ValueError(f"Export username does not match @{CHANNEL}")
    if not isinstance(chat.get("messages"), list):
        raise ValueError("Export is missing its messages list")
    return chat


def exported_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(exported_text(part) for part in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return ""


def media_path(export_root: Path, message: dict[str, Any]) -> Path | None:
    value = message.get("file")
    if not isinstance(value, str) or not value or value.startswith("(File not included") or value.startswith("File not included"):
        return None
    relative = Path(value.replace("\\", "/"))
    if relative.is_absolute():
        raise ValueError(f"Absolute media path is not accepted for message {message.get('id')}")
    resolved = (export_root / relative).resolve()
    if not resolved.is_relative_to(export_root.resolve()):
        raise ValueError(f"Media path leaves export directory for message {message.get('id')}")
    return resolved if resolved.is_file() else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def target_for(record: dict[str, Any], source: Path) -> Path:
    previous = safe_catalog_path(record.get("file"))
    if previous and previous.is_relative_to(ROOT / "content" / record["kind"]):
        return previous
    if record["kind"] == "video":
        suffix = ".mp4"
    else:
        suffix = source.suffix.lower()
        if suffix not in SUFFIXES:
            suffix = Path(str(record.get("original_filename") or "")).suffix.lower()
        if suffix not in SUFFIXES:
            suffix = ".m4a" if record.get("mime_type") == "audio/mp4" else ".mp3"
    return ROOT / "content" / record["kind"] / f"{record['message_id']}{suffix}"


def publish_original(source: Path, target: Path, expected: int, move: bool) -> None:
    if source.stat().st_size != expected:
        raise ValueError(f"Wrong original byte size for {source.name}: {source.stat().st_size}/{expected}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() == target.resolve():
        return
    if target.exists():
        if target.stat().st_size == expected and sha256(target) == sha256(source):
            if move:
                source.unlink()
            return
        raise ValueError(f"Existing archive file differs from export: {target.relative_to(ROOT)}")
    if move and source.stat().st_dev == target.parent.stat().st_dev:
        source.replace(target)
        return
    partial = target.with_suffix(target.suffix + ".desktop.part")
    shutil.copy2(source, partial)
    if partial.stat().st_size != expected:
        raise ValueError(f"Incomplete local transfer: {partial.name}")
    partial.replace(target)
    if move and source.exists() and source.resolve() != target.resolve():
        source.unlink()


def actual_mime(path: Path, kind: str, probe: dict[str, Any], fallback: str) -> str:
    format_name = probe.get("format", {}).get("format_name", "")
    if any(name in format_name.split(",") for name in ("mov", "mp4", "m4a")):
        return "video/mp4" if kind == "video" else "audio/mp4"
    return mimetypes.guess_type(path.name)[0] or fallback


def save_sidecar(record: dict[str, Any], exported_message: dict[str, Any] | None,
                 caption_source: dict[str, Any] | None = None) -> None:
    target = safe_catalog_path(record["file"])
    if target is None:
        raise ValueError("Unsafe archive path")
    record["metadata_file"] = target.with_suffix(".json").relative_to(ROOT).as_posix()
    record["caption_file"] = target.with_suffix(".txt").relative_to(ROOT).as_posix()
    payload = read_json(target.with_suffix(".json"), {})
    payload.update(record)
    if "record" in payload:
        payload["record"] = dict(record)
    if exported_message is not None:
        payload["telegram_desktop_message"] = exported_message
    if caption_source is not None:
        payload["telegram_desktop_caption_source"] = caption_source
    write_json_atomic(target.with_suffix(".json"), payload)
    text_path = target.with_suffix(".txt")
    temporary = text_path.with_suffix(".txt.tmp")
    temporary.write_text(record.get("description", ""), encoding="utf8")
    temporary.replace(text_path)


def archive_record(old_record: dict[str, Any], message: dict[str, Any] | None,
                   export_root: Path, move: bool,
                   caption_source: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    record = dict(old_record)
    expected = int(record.get("expected_size_bytes") or record.get("size_bytes") or 0)
    if expected <= 0:
        raise ValueError(f"No saved original byte size for message {record['message_id']}")
    source = media_path(export_root, message) if message is not None else None
    target = safe_catalog_path(record.get("file"))
    if source is None:
        if target is None:
            target = target_for(record, Path(str((message or {}).get("file") or record.get("original_filename") or "original")))
        if target is None or not target.is_file() or target.stat().st_size != expected:
            raise FileNotFoundError("Original media not present in export or archive")
    else:
        if source.stat().st_size != expected:
            raise ValueError(f"Export byte size mismatch: {source.stat().st_size}/{expected}")
        probe_media(source, record["kind"])
        target = target_for(record, source)
        publish_original(source, target, expected, move)
    assert target is not None
    probe = probe_media(target, record["kind"])
    if message is not None and "text" in message:
        caption = exported_text(message["text"])
        record["caption_original"] = caption
        if caption or record.get("caption_source_message_id", record["message_id"]) == record["message_id"]:
            record["description"] = caption
            record["caption_source_message_id"] = record["message_id"]
        elif caption_source is not None and "text" in caption_source:
            record["description"] = exported_text(caption_source["text"])
        record["tags"] = extract_tags(record.get("description", ""))
    record.update({
        "file": target.relative_to(ROOT).as_posix(), "size_bytes": target.stat().st_size,
        "duration_seconds": probe_duration(probe, record.get("duration_seconds", 0)),
        "mime_type": actual_mime(target, record["kind"], probe, record.get("mime_type", "")),
        "availability": "local", "sha256": sha256(target),
        "codecs": [{"type": stream["codec_type"], "codec": stream["codec_name"]}
                   for stream in probe["streams"] if stream.get("codec_type") in ("video", "audio")],
    })
    if source is not None:
        record["download_origin"] = "telegram_desktop_export"
    record.pop("download_error", None)
    records = [record]
    if record["kind"] == "video":
        poster = target.with_suffix(".jpg")
        create_poster(target, poster, record["duration_seconds"], regenerate=source is not None)
        audio, audio_probe = extract_audio(target, probe, ROOT / "content/audio" / f"{record['message_id']}-video")
        record["poster"] = poster.relative_to(ROOT).as_posix()
        record["audio_file"] = audio.relative_to(ROOT).as_posix()
        derivative = dict(record)
        derivative.update({
            "id": record["id"] + ":audio", "kind": "audio", "audio_origin": "extracted",
            "source_video_id": record["id"], "file": audio.relative_to(ROOT).as_posix(),
            "size_bytes": audio.stat().st_size, "mime_type": "audio/mp4" if audio.suffix == ".m4a" else "audio/mpeg",
            "duration_seconds": probe_duration(audio_probe), "sha256": sha256(audio),
            "codecs": [{"type": stream["codec_type"], "codec": stream["codec_name"]}
                       for stream in audio_probe["streams"] if stream.get("codec_type") == "audio"],
        })
        for field in ("audio_file", "original_filename", "expected_size_bytes", "download_origin"):
            derivative.pop(field, None)
        save_sidecar(derivative, message, caption_source)
        records.append(derivative)
    save_sidecar(record, message, caption_source)
    return records, {"message_id": record["message_id"], "file": record["file"],
                     "bytes": record["size_bytes"], "expected_bytes": expected,
                     "exact_size_match": record["size_bytes"] == expected, "sha256": record["sha256"]}


def save_progress(catalog: dict[str, Any], errors: list[dict[str, Any]], exported_count: int) -> None:
    now = utc_now()
    items = catalog["items"]
    for item in items:
        if item.get("availability") != "local":
            continue
        path = safe_catalog_path(item.get("file"))
        expected = int(item.get("expected_size_bytes") or item.get("size_bytes") or 0)
        if (path is None or not path.is_relative_to(ROOT / "content" / item["kind"])
                or not path.is_file() or expected <= 0 or path.stat().st_size != expected):
            item.update(availability="pending", file=None,
                        download_error="Local media is missing or does not match its saved original byte size")
    originals = [item for item in items if item.get("audio_origin") != "extracted"]
    pending = [item for item in originals if item.get("availability") != "local"]
    items_by_id = {item["id"]: item for item in items}
    pending_work = {item["message_id"] for item in originals if needs_repair(item, items_by_id)}
    missing_extractions = []
    for item in originals:
        if item["kind"] != "video" or item.get("availability") != "local":
            continue
        derivative = items_by_id.get(item["id"] + ":audio", {})
        audio_path = safe_catalog_path(derivative.get("file"))
        if audio_path is None or not audio_path.is_file():
            missing_extractions.append(item["message_id"])
    status = dict(catalog.get("import", {}))
    history_complete = bool(catalog.get("source", {}).get("history_complete", status.get("history_complete", False)))
    status.update({
        "status": "partial" if pending_work or errors or not history_complete else "complete", "pending_original_count": len(pending),
        "history_complete": history_complete,
        "pending_message_ids": sorted(pending_work), "pending_work_count": len(pending_work),
        "original_video_count": sum(item["kind"] == "video" for item in originals),
        "original_audio_count": sum(item["kind"] == "audio" for item in originals),
        "downloaded_original_video_count": sum(item["kind"] == "video" and item.get("availability") == "local" for item in originals),
        "downloaded_original_audio_count": sum(item["kind"] == "audio" and item.get("availability") == "local" for item in originals),
        "downloaded_original_bytes": sum(item["size_bytes"] for item in originals if item.get("availability") == "local"),
        "extracted_audio_count": sum(item.get("audio_origin") == "extracted" and item.get("availability") == "local" for item in items),
        "missing_extraction_message_ids": missing_extractions,
        "desktop_export_messages": exported_count, "desktop_import_errors": errors, "updated_at": now,
    })
    for field in ("blocked_reason", "blocker_detail"):
        status.pop(field, None)
    if errors:
        status.update(blocked_reason="desktop_import_failed", blocker_detail=errors[-1]["error"])
    catalog.update(updated_at=now, **{"import": status})
    catalog["items"].sort(key=lambda item: (item["published_at"], item["message_id"], item["kind"] == "video"), reverse=True)
    write_json_atomic(CATALOG, catalog)
    state = read_json(STATE, {"channels": {}})
    state.setdefault("channels", {})[CHANNEL] = status
    state["updated_at"] = now
    write_json_atomic(STATE, state)
    manifest = read_json(MANIFEST, {})
    manifest.update(updated_at=now, **{"import": status})
    verification = []
    for item in items:
        if item.get("availability") != "local":
            continue
        entry = {"id": item["id"], "message_id": item["message_id"], "file": item["file"], "bytes": item["size_bytes"], "sha256": item.get("sha256"), "duration_seconds": item.get("duration_seconds"), "codecs": item.get("codecs", [])}
        if item.get("audio_origin") == "extracted":
            original = items_by_id.get(item["source_video_id"], {})
            entry["source_duration_seconds"] = original.get("duration_seconds", 0)
            entry["duration_delta_seconds"] = abs(item["duration_seconds"] - entry["source_duration_seconds"])
        else:
            entry["expected_bytes"] = item.get("expected_size_bytes", item["size_bytes"])
            entry["exact_size_match"] = item["size_bytes"] == entry["expected_bytes"]
        verification.append(entry)
    manifest["verification"] = verification
    write_json_atomic(MANIFEST, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_json", type=Path, help="Telegram Desktop result.json")
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument("--copy", action="store_true", help="Copy originals and keep export media (default: move verified originals)")
    transfer.add_argument("--move", action="store_true", help="Move verified originals into content (default)")
    parser.add_argument("--dry-run", action="store_true", help="Verify channel, media paths and original sizes without modifying the archive")
    args = parser.parse_args()
    export_json = args.export_json.expanduser().resolve()
    chat = select_channel(read_json(export_json, {}))
    messages = {int(message["id"]): message for message in chat["messages"] if isinstance(message, dict) and "id" in message}
    catalog = read_json(CATALOG, {"items": []})
    originals = [dict(item) for item in catalog["items"] if item.get("channel") == CHANNEL and item.get("audio_origin") != "extracted"]
    if not originals:
        raise ValueError("Saved source inventory is missing; import metadata before using the Desktop export")
    inventoried_ids = {record["message_id"] for record in originals}
    unknown_media_ids = [message_id for message_id, message in messages.items() if message_id not in inventoried_ids and (str(message.get("mime_type", "")).startswith(("audio/", "video/")) or message.get("media_type") in ("video_file", "audio_file", "voice_message", "video_message"))]
    if unknown_media_ids:
        raise ValueError(f"Export has media absent from the saved inventory; refresh channel metadata first: {unknown_media_ids}")
    errors = []
    ready = 0
    for record in originals:
        message = messages.get(record["message_id"])
        try:
            if args.dry_run:
                source = media_path(export_json.parent, message) if message is not None else None
                source = source or safe_catalog_path(record.get("file"))
                expected = int(record.get("expected_size_bytes") or record.get("size_bytes") or 0)
                if source is None or not source.is_file() or source.stat().st_size != expected:
                    raise ValueError("Missing original media or byte size mismatch")
                ready += 1
                continue
            caption_source = messages.get(record.get("caption_source_message_id", record["message_id"]))
            imported, verification = archive_record(record, message, export_json.parent, not args.copy, caption_source)
            upsert_records(catalog["items"], imported)
            ready += 1
            save_progress(catalog, errors, len(messages))
        except (OSError, ValueError, RuntimeError) as error:
            errors.append({"message_id": record["message_id"], "error": str(error)})
            print(f"Message {record['message_id']}: {error}", file=sys.stderr)
        if ready and ready % 10 == 0:
            print(f"Verified originals: {ready}/{len(originals)}", flush=True)
    if not args.dry_run:
        save_progress(catalog, errors, len(messages))
    print(json.dumps({"channel": CHANNEL, "dry_run": args.dry_run, "inventoried_originals": len(originals), "verified_originals": ready, "errors": errors}, ensure_ascii=False, indent=2))
    return 1 if errors or (not args.dry_run and catalog["import"]["status"] != "complete") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Desktop import failed: {error}", file=sys.stderr)
        raise SystemExit(1)
