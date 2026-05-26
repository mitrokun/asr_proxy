import asyncio
import contextvars
import logging
import time
from typing import AsyncIterable, List, Optional, Callable

from homeassistant.components import stt
from homeassistant.components.stt import (
    AudioBitRates,
    AudioChannels,
    AudioCodecs,
    AudioFormats,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from wyoming.asr import Transcribe, Transcript, TranscriptChunk
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error

from .api import SttApi
from .const import (
    DOMAIN,
    SAMPLE_CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    CONF_SPEECH_TO_PHRASE,
)

_LOGGER = logging.getLogger(__name__)

# Maximum time to wait for server response (chunks or final) before aborting
INACTIVITY_TIMEOUT = 10.0

# Timeout for quick connection check
CONNECTION_TIMEOUT = 0.064

# Total operation timeout for buffered mode fallback
BUFFERED_OPERATION_TIMEOUT = 10.0


def get_stt_stream_callback_var(hass: HomeAssistant) -> contextvars.ContextVar:
    """Get or create the global STT streaming context variable.
    
    This shared context variable allows any calling application to register 
    a generic callback to receive real-time text chunks.
    """
    if "stt_stream_callback_var" not in hass.data:
        hass.data["stt_stream_callback_var"] = contextvars.ContextVar(
            "stt_stream_callback", default=None
        )
    return hass.data["stt_stream_callback_var"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ASR Proxy platform from a config entry."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        [
            AsrProxyProvider(
                config_entry.entry_id,
                entry_data["primary_api"],
                entry_data.get("fallback_api"),
                config_entry,
                hass,
            )
        ]
    )


async def _check_connection(api: SttApi, timeout: float) -> bool:
    """Quickly checks if a server is connectable."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(api.host, api.port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError):
        return False


class AsrProxyProvider(stt.SpeechToTextEntity):
    """An ASR provider with configurable buffering and fallback logic."""

    def __init__(
        self,
        unique_id: str,
        primary_api: SttApi,
        fallback_api: Optional[SttApi],
        config_entry: ConfigEntry,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the ASR Proxy Provider."""
        self._attr_unique_id = unique_id
        self._attr_name = f"ASR Proxy ({primary_api.host})"
        self.primary_api = primary_api
        self.fallback_api = fallback_api
        self._config_entry = config_entry
        self.hass = hass

    @property
    def supported_languages(self) -> list[str]:
        return ["en", "fr", "de", "nl", "es", "it", "ru", "cs", "ca", "el", 
                "ro", "pt", "pl", "hi", "eu", "fi", "mn", "sl", "sw", "th", "tr"]

    @property
    def supported_formats(self) -> list[AudioFormats]:
        return [AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[AudioCodecs]:
        return [AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[AudioBitRates]:
        return [AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[AudioSampleRates]:
        return [AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self) -> list[AudioChannels]:
        return [AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Processes audio stream using buffering (legacy) or streaming (low latency)."""
        use_buffering = self._config_entry.options.get(CONF_SPEECH_TO_PHRASE, False)

        if use_buffering:
            _LOGGER.debug("Buffering mode enabled (High reliability, Low speed)")
            return await self._process_audio_buffered(metadata, stream)
        
        _LOGGER.debug("Streaming mode enabled (Low latency)")
        return await self._process_audio_streamed(metadata, stream)

    async def _process_audio_buffered(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Buffers all audio then tries primary, failing over to fallback."""
        try:
            audio_chunks = [chunk async for chunk in stream]
        except asyncio.CancelledError:
            return SpeechResult(None, SpeechResultState.ERROR)

        # 1. Try Primary
        if await _check_connection(self.primary_api, CONNECTION_TIMEOUT):
            try:
                result = await asyncio.wait_for(
                    self._try_transcribe_buffered(self.primary_api, metadata, audio_chunks),
                    timeout=BUFFERED_OPERATION_TIMEOUT
                )
                if result and result.strip():
                    return SpeechResult(result, SpeechResultState.SUCCESS)
                _LOGGER.debug("Primary returned empty result. Failing over.")
            except Exception as e:
                _LOGGER.warning("Primary server failed (%s). Failing over.", e)

        # 2. Try Fallback
        if self.fallback_api:
            _LOGGER.debug("Attempting fallback: %s", self.fallback_api.host)
            try:
                result = await asyncio.wait_for(
                    self._try_transcribe_buffered(self.fallback_api, metadata, audio_chunks),
                    timeout=BUFFERED_OPERATION_TIMEOUT
                )
                if result:
                    return SpeechResult(result, SpeechResultState.SUCCESS)
            except Exception as e:
                _LOGGER.error("Fallback server failed: %s", e)
        
        return SpeechResult("", SpeechResultState.SUCCESS)

    async def _process_audio_streamed(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Streams audio to available server with smart watchdog."""
        target_api = None
        
        # Determine target server
        if await _check_connection(self.primary_api, CONNECTION_TIMEOUT):
            target_api = self.primary_api
        elif self.fallback_api and await _check_connection(self.fallback_api, CONNECTION_TIMEOUT):
            _LOGGER.debug("Primary unreachable, using fallback: %s", self.fallback_api.host)
            target_api = self.fallback_api
        
        if not target_api:
            _LOGGER.error("No STT servers available.")
            return SpeechResult(None, SpeechResultState.ERROR)

        try:
            result_text = await self._concurrent_stream_transcribe(target_api, metadata, stream)

            if not result_text or not result_text.strip():
                _LOGGER.debug("Empty result from %s", target_api.host)
                return SpeechResult(None, SpeechResultState.ERROR)

            return SpeechResult(result_text, SpeechResultState.SUCCESS)

        except asyncio.CancelledError:
            # Expected during VAD interruption
            return SpeechResult(None, SpeechResultState.ERROR)
        except Exception as e:
            _LOGGER.error("Streaming error on %s: %s", target_api.host, e)
            return SpeechResult(None, SpeechResultState.ERROR)

    async def _concurrent_stream_transcribe(
        self, api: SttApi, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> Optional[str]:
        """Bi-directional streaming with activity watchdog."""
        text_result: Optional[str] = None
        last_activity_time = time.time()

        def update_activity():
            nonlocal last_activity_time
            last_activity_time = time.time()

        writer_task = None
        reader_task = None

        try:
            async with AsyncTcpClient(api.host, api.port) as client:
                # Initial headers
                await client.write_event(Transcribe(language=metadata.language).event())
                await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS).event())

                # Start background tasks
                reader_task = asyncio.create_task(
                    self._read_transcript_from_client(client, api, update_activity)
                )
                writer_task = asyncio.create_task(self._send_audio_to_client(client, stream))

                while not reader_task.done():
                    # Wait for EITHER reader or writer to finish.
                    # Timeout ensures we wake up to check the watchdog.
                    tasks_to_wait = {reader_task}
                    if writer_task:
                        tasks_to_wait.add(writer_task)

                    done, _ = await asyncio.wait(
                        tasks_to_wait,
                        timeout=0.5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Case A: Server returned a Final Transcript (Reader done)
                    if reader_task in done:
                        text_result = reader_task.result()
                        break

                    # Case B: Inactivity Watchdog
                    if time.time() - last_activity_time > INACTIVITY_TIMEOUT:
                        _LOGGER.warning("Server %s inactive for %.1fs. Aborting.", api.host, INACTIVITY_TIMEOUT)
                        break

                    # Case C: Audio Stream Ended
                    if writer_task and writer_task in done:
                        if not writer_task.cancelled() and not writer_task.exception():
                            _LOGGER.debug("Audio sent to %s. Waiting for processing...", api.host)
                            await client.write_event(AudioStop().event())
                            writer_task = None
                        else:
                            _LOGGER.warning("Audio upload failed or cancelled.")
                            break

        except Exception as e:
            _LOGGER.error("Stream exception with %s: %s", api.host, e)
            raise
        finally:
            # Cleanup tasks
            if writer_task and not writer_task.done():
                writer_task.cancel()
            if reader_task and not reader_task.done():
                reader_task.cancel()
        
        return text_result

    async def _read_transcript_from_client(
        self, 
        client: AsyncTcpClient, 
        api: SttApi, 
        on_activity: Callable[[], None]
    ) -> Optional[str]:
        """Reads events from server, updates watchdog, handles chunks."""
        # Retrieve the global ContextVar from Home Assistant data registry
        callback_var = get_stt_stream_callback_var(self.hass)

        while True:
            event = await client.read_event()
            if event is None:
                _LOGGER.debug("Connection closed by %s", api.host)
                return None
            
            # Reset watchdog on ANY valid event from server
            on_activity()

            if TranscriptChunk.is_type(event.type):
                chunk = TranscriptChunk.from_event(event)
                
                # Check if a streaming callback is registered in the current async context
                callback = callback_var.get()
                if callback and chunk.text:
                    try:
                        # Forward the raw chunk to the caller-defined callback
                        callback(chunk.text)
                    except Exception as e:
                        _LOGGER.error("Error executing STT stream callback: %s", e)
                continue

            # Final result
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text

            if Error.is_type(event.type):
                _LOGGER.warning("Server %s error: %s", api.host, Error.from_event(event).text)
                return None

    async def _send_audio_to_client(self, client: AsyncTcpClient, stream: AsyncIterable[bytes]) -> None:
        """Streams audio chunks to the server."""
        async for audio_bytes in stream:
            chunk = AudioChunk(
                rate=SAMPLE_RATE, 
                width=SAMPLE_WIDTH, 
                channels=SAMPLE_CHANNELS, 
                audio=audio_bytes
            )
            await client.write_event(chunk.event())

    async def _try_transcribe_buffered(
        self, api: SttApi, metadata: SpeechMetadata, audio_chunks: List[bytes]
    ) -> Optional[str]:
        """One-shot transcription for buffered mode."""
        try:
            async with AsyncTcpClient(api.host, api.port) as client:
                await client.write_event(Transcribe(language=metadata.language).event())
                await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS).event())
                
                for audio_bytes in audio_chunks:
                    await client.write_event(AudioChunk(
                        rate=SAMPLE_RATE, 
                        width=SAMPLE_WIDTH, 
                        channels=SAMPLE_CHANNELS, 
                        audio=audio_bytes
                    ).event())
                
                await client.write_event(AudioStop().event())

                while True:
                    event = await client.read_event()
                    if event is None: return None
                    if Error.is_type(event.type):
                        return None
                    if Transcript.is_type(event.type):
                        return Transcript.from_event(event).text
        except Exception:
            return None
