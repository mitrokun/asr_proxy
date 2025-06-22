# --- stt.py ---

import asyncio
import logging
from typing import AsyncIterable, List, Optional

# Imports from Home Assistant stt component
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

# Imports from the wyoming library
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error

# Imports from our integration
from .api import CannotConnect, SttApi
from .const import DOMAIN, SAMPLE_CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH

_LOGGER = logging.getLogger(__name__)

# Timeout for establishing a TCP connection to the primary server.
# If the server doesn't accept the connection within this time, it's considered unavailable.
PRIMARY_CONNECT_TIMEOUT = 0.05  # seconds

# General timeout for the entire ASR operation on any given server.
# This prevents the pipeline from hanging if a connected server becomes unresponsive.
OPERATION_TIMEOUT = 10  # seconds


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
            )
        ]
    )

async def _check_connection(api: SttApi, timeout: float) -> bool:
    """Quickly checks if a server is connectable within a timeout."""
    _LOGGER.debug("Checking connection to %s (timeout: %.1fs)", api.host, timeout)
    try:
        # Attempt to open and immediately close a connection
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(api.host, api.port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        _LOGGER.debug("Server %s is available.", api.host)
        return True
    except (asyncio.TimeoutError, OSError) as e:
        _LOGGER.debug("Server %s is unavailable: %s", api.host, e)
        return False


class AsrProxyProvider(stt.SpeechToTextEntity):
    """
    An ASR provider that uses a primary server and can fall back to a secondary
    server on failure or if the primary returns an empty result.
    """

    def __init__(
        self,
        unique_id: str,
        primary_api: SttApi,
        fallback_api: Optional[SttApi],
    ) -> None:
        """Initialize the provider."""
        self._attr_unique_id = unique_id
        self._attr_name = f"ASR Proxy ({primary_api.host})"
        self.primary_api = primary_api
        self.fallback_api = fallback_api

    @property
    def supported_languages(self) -> list[str]:
        return ["en", "fr", "de", "nl", "es", "it", "ru", "cs", "ca", "el", "ro", "pt", "pl", "hi", "eu", "fi", "mn", "sl", "sw", "th", "tr"]

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
        """
        Processes an audio stream with a two-stage ASR approach:
        1. Tries a fast primary server.
        2. If the primary is unavailable, fails, or returns an empty result,
           it falls back to a secondary, potentially more accurate, server.
        """
        try:
            audio_chunks = [chunk async for chunk in stream]
        except asyncio.CancelledError:
            _LOGGER.debug("Audio stream reading was cancelled.")
            return SpeechResult(None, SpeechResultState.ERROR)
        _LOGGER.debug("Audio stream cached, chunks: %d", len(audio_chunks))

        # --- Stage 1: Check and attempt transcription on the primary server ---
        primary_is_available = await _check_connection(self.primary_api, PRIMARY_CONNECT_TIMEOUT)
        
        primary_failed_or_empty = True  # Assume failure until proven otherwise

        if primary_is_available:
            _LOGGER.debug("Primary server is available, attempting transcription.")
            try:
                result = await asyncio.wait_for(
                    self._try_transcribe(self.primary_api, metadata, audio_chunks),
                    timeout=OPERATION_TIMEOUT
                )
                
                if result is not None:
                    if result.strip():
                        # Success: result is not None and not an empty string.
                        _LOGGER.debug("Primary server successfully transcribed text.")
                        primary_failed_or_empty = False
                        return SpeechResult(result, SpeechResultState.SUCCESS)
                    
                    # Failure case: server returned an empty string.
                    _LOGGER.debug("Primary server returned an empty result. Failing over.")
                else:
                    # Failure case: server returned None (protocol error, etc.).
                    _LOGGER.warning("Primary server was available but failed to process. Failing over.")
            
            except asyncio.TimeoutError:
                _LOGGER.warning("Primary server was available but timed out after %ds. Failing over.", OPERATION_TIMEOUT)
            except Exception as e:
                _LOGGER.warning("An error occurred with the primary server (%s). Failing over.", e)
        else:
            _LOGGER.debug("Primary server is not available. Skipping to fallback.")

        # --- Stage 2: If primary failed or returned empty, use the fallback server ---
        if primary_failed_or_empty and self.fallback_api:
            _LOGGER.debug("Failing over to fallback server: %s", self.fallback_api.host)
            try:
                # We don't need a quick check for the fallback, we trust it.
                result = await asyncio.wait_for(
                    self._try_transcribe(self.fallback_api, metadata, audio_chunks),
                    timeout=OPERATION_TIMEOUT
                )
                if result is not None:
                    # We accept any result from the fallback, even an empty one.
                    return SpeechResult(result, SpeechResultState.SUCCESS)
            except Exception as e:
                _LOGGER.error("Fallback server also failed: %s", e)

        _LOGGER.error("Failed to transcribe speech. All configured servers failed or were unavailable.")
        return SpeechResult(None, SpeechResultState.ERROR)


    async def _try_transcribe(
        self,
        api: SttApi,
        metadata: SpeechMetadata,
        audio_chunks: List[bytes],
    ) -> Optional[str]:
        """Performs a single, reliable ASR attempt with a given server."""
        try:
            async with AsyncTcpClient(api.host, api.port) as client:
                await client.write_event(Transcribe(language=metadata.language).event())
                await client.write_event(
                    AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS).event()
                )
                for audio_bytes in audio_chunks:
                    await client.write_event(
                        AudioChunk(
                            rate=SAMPLE_RATE,
                            width=SAMPLE_WIDTH,
                            channels=SAMPLE_CHANNELS,
                            audio=audio_bytes,
                        ).event()
                    )
                await client.write_event(AudioStop().event())

                while True:
                    event = await client.read_event()
                    if event is None:
                        _LOGGER.warning("Connection to %s closed by server.", api.host)
                        return None
                    if Error.is_type(event.type):
                        error = Error.from_event(event)
                        _LOGGER.warning("Server %s returned an error: %s", api.host, error.text)
                        return None
                    if Transcript.is_type(event.type):
                        transcript = Transcript.from_event(event)
                        # The result can be an empty string, which is a valid response.
                        return transcript.text
            return None
        except Exception:
            # Re-raise the exception to be caught by the calling function.
            # This allows the caller to distinguish between a failed attempt
            # and a successful attempt that returned None.
            raise
