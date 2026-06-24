from __future__ import annotations

import asyncio
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

import structlog

from ghkge.harvesters.base import BaseHarvester
from ghkge.models.schemas import RawCaptureData

logger = structlog.get_logger()

_media_executor = ThreadPoolExecutor(max_workers=1)


class MediaHarvester(BaseHarvester):
    """Audio/Video harvester using yt-dlp + faster-whisper."""

    async def fetch(self, url: str, run_id: uuid.UUID, **kwargs: object) -> RawCaptureData | None:
        compliance_result = await self._check_compliance(
            url, strategy="media_transcripts", entity_type=kwargs.get("entity_type", "")
        )
        if not compliance_result.allowed:
            logger.info("media_harvester.blocked", url=url, reason=compliance_result.reason)
            return None

        try:
            content = await asyncio.get_event_loop().run_in_executor(
                _media_executor, self._download_and_transcribe, url
            )
            if content is None:
                return None

            return RawCaptureData(
                source_url=url,
                source_type="audio",
                domain=self.extract_domain(url),
                raw_content=content,
                content_hash=self.compute_content_hash(content),
                strategy_used="media_transcripts",
                run_id=run_id,
            )
        except Exception:
            logger.error("media_harvester.fetch_error", url=url, exc_info=True)
            return None

    def _download_and_transcribe(self, url: str) -> str | None:
        """Download audio and transcribe. Runs in thread pool."""
        try:
            import yt_dlp

            with tempfile.TemporaryDirectory() as tmpdir:
                output_path = f"{tmpdir}/audio.%(ext)s"
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": output_path,
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info is None:
                        return None
                    ydl.download([url])

                # Find the downloaded file
                import glob

                files = glob.glob(f"{tmpdir}/audio.*")
                if not files:
                    return None

                audio_path = files[0]
                return self._transcribe(audio_path)
        except ImportError:
            logger.warning("media_harvester.ytdlp_not_installed")
            return None
        except Exception:
            logger.error("media_harvester.download_error", url=url, exc_info=True)
            return None

    def _transcribe(self, audio_path: str) -> str | None:
        """Transcribe audio using faster-whisper."""
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel("large-v3-turbo", compute_type="int8")
            segments, info = model.transcribe(audio_path, beam_size=5)
            text_parts = [segment.text for segment in segments]
            return " ".join(text_parts)
        except ImportError:
            logger.warning("media_harvester.whisper_not_installed")
            return None
        except Exception:
            logger.error("media_harvester.transcribe_error", exc_info=True)
            return None
