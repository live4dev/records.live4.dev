"""Offline regression tests for complete captions, resume, and verified extraction."""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone


SPEC = importlib.util.spec_from_file_location("telegram_import", Path(__file__).with_name("telegram_import.py"))
archive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = archive
SPEC.loader.exec_module(archive)


def message(message_id, text="", kind="video", grouped_id=None, size=8):
    return SimpleNamespace(
        id=message_id, raw_text=text, date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        file=SimpleNamespace(mime_type=f"{kind}/mp4", name=f"original-{message_id}.mp4", size=size),
        video=kind == "video", audio=kind == "audio", voice=False,
        media=f"media-{message_id}", document=SimpleNamespace(id=1000 + message_id, attributes=[]),
        grouped_id=grouped_id, entities=[],
    )


class FakeClient:
    def __init__(self, messages=(), data=b"abcdefgh"):
        self.messages = {item.id: item for item in messages}
        self.data = data
        self.history_calls = []
        self.download_offsets = []
        self.fail_download_once = False

    async def get_entity(self, channel):
        return channel

    async def iter_messages(self, entity, min_id=0, limit=None):
        self.history_calls.append((min_id, limit))
        selected = sorted((item for item in self.messages.values() if item.id > min_id),
                          key=lambda item: item.id, reverse=True)
        for item in selected[:limit]:
            yield item

    async def get_messages(self, entity, ids):
        return [self.messages.get(item_id) for item_id in ids]

    async def iter_download(self, media, offset=0, **kwargs):
        self.download_offsets.append(offset)
        data = self.data[offset:]
        if self.fail_download_once:
            self.fail_download_once = False
            yield data[:3]
            raise RuntimeError("Connection interrupted")
        yield data


class ImportTests(unittest.TestCase):
    def config(self, **overrides):
        fields = dict(api_id=1, api_hash="test", session="test", channels=("records_live4dev",),
                      history_limit=None, max_file_bytes=None, dry_run=False)
        fields.update(overrides)
        return archive.Config(**fields)

    def test_unlimited_defaults_and_source_scope(self):
        args = argparse.Namespace(channels=None, dry_run=True, full_history=False, refresh=False)
        env = {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "test", "TELEGRAM_SESSION": "test"}
        with patch.dict(os.environ, env, clear=True):
            config = archive.Config.from_environment(args)
            self.assertIsNone(config.history_limit)
            self.assertIsNone(config.max_file_bytes)
            self.assertEqual(config.channels, ("records_live4dev",))
            with patch.dict(os.environ, {"TELEGRAM_MAX_FILE_MB": "90"}):
                self.assertEqual(archive.Config.from_environment(args).max_file_bytes, 90 * 1024 * 1024)
            args.channels = "mp3_live4dev"
            with self.assertRaisesRegex(ValueError, "only"):
                archive.Config.from_environment(args)

    def test_long_caption_and_album_inheritance_preserve_original(self):
        caption = "  Заголовок\n\n" + ("Длинный текст  " * 120) + "\n#Музыка #LIVE #музыка"
        original = message(1, caption, grouped_id=77)
        sibling = message(2, "", grouped_id=77)
        source = archive.album_captions([sibling, original])[77]
        record = archive.build_record(sibling, "records_live4dev", "video", {}, "content/video/2.mp4", source)
        self.assertEqual(record["description"], caption)
        self.assertEqual(record["caption_original"], "")
        self.assertEqual(record["caption_source_message_id"], 1)
        self.assertEqual(record["tags"], ["Музыка", "LIVE"])
        self.assertEqual(record["title"], "Заголовок")
        self.assertEqual(archive.build_record(original, "records_live4dev", "video", {}, "x")["caption_original"], caption)

    def test_interrupted_download_resumes_exact_bytes(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "1.mp4"
                client = FakeClient()
                client.fail_download_once = True
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    await archive.download_original(client, message(1), target)
                self.assertFalse(target.exists())
                self.assertEqual(target.with_suffix(".mp4.part").read_bytes(), b"abc")
                await archive.download_original(client, message(1), target)
                self.assertEqual(client.download_offsets, [0, 3])
                self.assertEqual(target.read_bytes(), client.data)
                self.assertFalse(target.with_suffix(".mp4.part").exists())
        asyncio.run(scenario())

    def test_download_identity_change_does_not_resume_old_document(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "1.mp4"
                partial = target.with_suffix(".mp4.part")
                partial.write_bytes(b"old")
                archive.write_json_atomic(partial.with_suffix(".part.json"), {"document_id": 44, "size_bytes": 8})
                client = FakeClient()
                await archive.download_original(client, message(1), target)
                self.assertEqual(client.download_offsets, [0])
                self.assertEqual(target.read_bytes(), b"abcdefgh")
        asyncio.run(scenario())

    def test_failure_keeps_prior_success_without_skipping_checkpoint(self):
        async def scenario():
            first, failed, later = message(1), message(2), message(3)
            client = FakeClient([first, failed, later])
            state = {"last_message_id": 99}
            items = []
            saved = []

            async def download(client, item, channel, kind, metadata, dry_run, source, old):
                if item.id == 2:
                    raise RuntimeError("ffmpeg failed")
                return [archive.build_record(item, channel, kind, metadata, f"content/video/{item.id}.mp4")]

            with patch.object(archive, "download_record", side_effect=download):
                with self.assertRaisesRegex(RuntimeError, "Failed @records_live4dev/2"):
                    await archive.import_channel(client, self.config(), "records_live4dev", items, state,
                                                 lambda: saved.append((len(items), state.copy())))
            self.assertEqual(client.history_calls, [(0, None)])
            self.assertEqual(state["last_message_id"], 1)
            self.assertEqual(state["failed_message_id"], 2)
            self.assertFalse(state["archive_complete"])
            self.assertEqual([item["message_id"] for item in items], [1])
            self.assertEqual(saved[-1][0], 1)
        asyncio.run(scenario())

    def test_explicit_size_cap_is_error_without_checkpoint(self):
        async def scenario():
            state = {}
            with self.assertRaisesRegex(RuntimeError, "exceeds TELEGRAM_MAX_FILE_MB"):
                await archive.import_channel(FakeClient([message(1, size=20)]), self.config(max_file_bytes=10),
                                             "records_live4dev", [], state)
            self.assertEqual(state["last_message_id"], 0)
            self.assertEqual(state["failed_message_id"], 1)
        asyncio.run(scenario())

    def test_refresh_replaces_metadata_and_dry_run_writes_nothing(self):
        async def scenario():
            state = {"initialized": True, "archive_complete": True, "last_message_id": 4}
            old_state = state.copy()
            items = [{"id": "records_live4dev:1", "description": "old"}]
            client = FakeClient([message(1, "Edited\n#tag")])
            with patch.object(archive, "write_json_atomic") as write:
                added, _ = await archive.import_channel(client, self.config(refresh=True, dry_run=True),
                                                       "records_live4dev", items, state)
                write.assert_not_called()
            self.assertEqual(added, 0)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["description"], "Edited\n#tag")
            self.assertEqual(state, old_state)
            self.assertEqual(client.history_calls, [(0, None)])
        asyncio.run(scenario())

    def test_incremental_repairs_pending_items_before_checkpoint(self):
        async def scenario():
            state = {"initialized": True, "archive_complete": True, "last_message_id": 50}
            pending = {"id": "records_live4dev:2", "channel": "records_live4dev", "message_id": 2,
                       "kind": "video", "file": None}
            client = FakeClient([message(2, "Repaired")])
            with patch.object(archive, "download_record", new=AsyncMock(return_value=[{**pending, "file": "x"}])) as download:
                with patch.object(archive, "needs_repair", side_effect=lambda record, items: record.get("file") is None):
                    await archive.import_channel(client, self.config(), "records_live4dev", [pending], state)
                self.assertEqual(download.await_args.args[1].id, 2)
            self.assertEqual(state["last_message_id"], 50)
        asyncio.run(scenario())

    def test_progress_summary_clears_stale_blocker_and_includes_missing_derivative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "content/video/1.mp4"
            video.parent.mkdir(parents=True)
            for path in (video, video.with_suffix(".jpg"), video.with_suffix(".json")):
                path.write_bytes(b"fixture")
            video.with_suffix(".txt").write_text("")
            item = {"id": "records_live4dev:1", "channel": "records_live4dev", "message_id": 1,
                    "kind": "video", "file": "content/video/1.mp4", "size_bytes": 7,
                    "poster": "content/video/1.jpg", "audio_file": "content/audio/1-video.m4a"}
            catalog = {"import": {"blocked_reason": "telegram_connector_empty_mcp_roots",
                                   "blocker_detail": "stale"}}
            state = {"history_complete": True}
            with patch.object(archive, "ROOT", root):
                summary = archive.update_catalog_summary(catalog, [item], state)
                self.assertEqual(summary["downloaded_original_video_count"], 1)
                self.assertEqual(summary["pending_original_count"], 0)
                self.assertEqual(summary["pending_message_ids"], [1])
                self.assertEqual(summary["status"], "partial")
                self.assertNotIn("blocked_reason", summary)
                audio = root / item["audio_file"]
                audio.parent.mkdir(parents=True)
                for path in (audio, audio.with_suffix(".json")):
                    path.write_bytes(b"fixture")
                audio.with_suffix(".txt").write_text("")
                derivative = {**item, "id": item["id"] + ":audio", "kind": "audio",
                              "source_video_id": item["id"], "file": item["audio_file"]}
                summary = archive.update_catalog_summary(catalog, [item, derivative], state)
                self.assertEqual(summary["status"], "complete")
                self.assertEqual(summary["extracted_audio_count"], 1)
                self.assertEqual(summary["pending_work_count"], 0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class MediaIntegrationTests(unittest.TestCase):
    def test_aac_copy_mp3_conversion_posters_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for codec, suffix in (("aac", ".mp4"), ("pcm_s16le", ".mkv")):
                video = root / (codec + suffix)
                archive.run_media_command([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", codec, str(video),
                ])
                probe = archive.probe_media(video, "video")
                poster = root / (codec + ".jpg")
                archive.create_poster(video, poster, archive.probe_duration(probe))
                self.assertGreater(poster.stat().st_size, 0)
                audio, audio_probe = archive.extract_audio(video, probe, root / (codec + "-video"))
                self.assertEqual(audio.suffix, ".m4a" if codec == "aac" else ".mp3")
                self.assertAlmostEqual(archive.probe_duration(audio_probe), 2, delta=0.2)
                mtime = audio.stat().st_mtime_ns
                repeated, _ = archive.extract_audio(video, probe, root / (codec + "-video"))
                self.assertEqual(repeated.stat().st_mtime_ns, mtime)
                # A truncated derivative gets regenerated instead of being trusted.
                audio.write_bytes(b"invalid")
                archive.extract_audio(video, probe, root / (codec + "-video"))
                archive.probe_media(audio, "audio")


if __name__ == "__main__":
    unittest.main()
