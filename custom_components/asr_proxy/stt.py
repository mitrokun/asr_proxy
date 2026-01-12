import asyncio
import logging
from typing import AsyncIterable, List, Optional

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

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error

from .api import CannotConnect, SttApi
from .const import (
    DOMAIN,
    SAMPLE_CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    CONF_SPEECH_TO_PHRASE,
)

_LOGGER = logging.getLogger(__name__)

# Таймаут для быстрой проверки доступности основного сервера
PRIMARY_CONNECT_TIMEOUT = 0.064  # секунды

# Таймаут для всей операции в БУФЕРИЗИРОВАННОМ режиме.
OPERATION_TIMEOUT = 10  # секунды

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
            )
        ]
    )

async def _check_connection(api: SttApi, timeout: float) -> bool:
    """Quickly checks if a server is connectable within a timeout."""
    _LOGGER.debug("Checking connection to %s (timeout: %.3fs)", api.host, timeout)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(api.host, api.port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        _LOGGER.debug("Server %s is available.", api.host)
        return True
    except (asyncio.TimeoutError, OSError) as e:
        _LOGGER.debug("Server %s is unavailable: %s", api.host, e)
        return False

class AsrProxyProvider(stt.SpeechToTextEntity):
    """An ASR provider with configurable buffering and fallback logic."""

    def __init__(
        self,
        unique_id: str,
        primary_api: SttApi,
        fallback_api: Optional[SttApi],
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the provider."""
        self._attr_unique_id = unique_id
        self._attr_name = f"ASR Proxy ({primary_api.host})"
        self.primary_api = primary_api
        self.fallback_api = fallback_api
        self._config_entry = config_entry

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
        """Processes an audio stream using either buffering or streaming mode."""
        use_buffering = self._config_entry.options.get(CONF_SPEECH_TO_PHRASE, False)

        if use_buffering:
            _LOGGER.debug("Speech2Phrase mode enabled. Using buffering for reliable fallback.")
            return await self._process_audio_buffered(metadata, stream)
        
        _LOGGER.debug("Streaming mode enabled for low latency.")
        return await self._process_audio_streamed(metadata, stream)

    async def _process_audio_buffered(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Handles audio with full buffering to allow fallback on empty results."""
        try:
            audio_chunks = [chunk async for chunk in stream]
        except asyncio.CancelledError:
            return SpeechResult(None, SpeechResultState.ERROR)
        _LOGGER.debug("Audio stream cached, chunks: %d", len(audio_chunks))

        primary_is_available = await _check_connection(self.primary_api, PRIMARY_CONNECT_TIMEOUT)
        
        if primary_is_available:
            _LOGGER.debug("Primary server is available, attempting transcription.")
            try:
                result = await asyncio.wait_for(
                    self._try_transcribe(self.primary_api, metadata, audio_chunks),
                    timeout=OPERATION_TIMEOUT
                )
                if result and result.strip():
                    return SpeechResult(result, SpeechResultState.SUCCESS)
                _LOGGER.debug("Primary server returned an empty result. Failing over.")
            except (asyncio.TimeoutError, Exception) as e:
                _LOGGER.warning("Error with primary server (%s). Failing over.", e)

        if self.fallback_api:
            _LOGGER.debug("Failing over to fallback server: %s", self.fallback_api.host)
            try:
                result = await asyncio.wait_for(
                    self._try_transcribe(self.fallback_api, metadata, audio_chunks),
                    timeout=OPERATION_TIMEOUT
                )
                if result is not None:
                    return SpeechResult(result, SpeechResultState.SUCCESS)
            except Exception as e:
                _LOGGER.error("Fallback server also failed: %s", e)
        
        return SpeechResult("", SpeechResultState.SUCCESS)

    async def _process_audio_streamed(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        """Handles audio with streaming for low latency, fallback on connection error only."""
        target_api = None
        
        if await _check_connection(self.primary_api, PRIMARY_CONNECT_TIMEOUT):
            _LOGGER.debug("Primary server is available, streaming to it.")
            target_api = self.primary_api
        elif self.fallback_api and await _check_connection(self.fallback_api, PRIMARY_CONNECT_TIMEOUT):
            _LOGGER.debug("Primary unavailable, streaming to fallback server.")
            target_api = self.fallback_api
        
        if not target_api:
            _LOGGER.error("No available servers to stream to.")
            return SpeechResult(None, SpeechResultState.ERROR)

        try:
            result_text = await self._concurrent_stream_transcribe(target_api, metadata, stream)


            # Проверяем на None (ошибка сокета) ИЛИ на пустую строку/пробелы
            if not result_text or not result_text.strip():
                _LOGGER.debug(
                    "Transcription returned empty result ('%s') from %s. Stopping pipeline.",
                    result_text,
                    target_api.host,
                )
                return SpeechResult(None, SpeechResultState.ERROR)

            return SpeechResult(result_text, SpeechResultState.SUCCESS)

        except asyncio.CancelledError:
            # Это ожидаемое исключение от быстрого VAD. Логируем как DEBUG.
            _LOGGER.debug(
                "Concurrent streaming to %s was cancelled as expected.", target_api.host
            )
            return SpeechResult(None, SpeechResultState.ERROR)

        except Exception as e:
            # Настоящая ошибка. Логируем как ERROR.
            _LOGGER.error("Error during concurrent streaming to %s: %s", target_api.host, e)
            return SpeechResult(None, SpeechResultState.ERROR)

    async def _concurrent_stream_transcribe(
        self, api: SttApi, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> Optional[str]:
        """Concurrently sends audio and listens for a transcript."""
        text_result: Optional[str] = None
        writer_task = None
        reader_task = None

        try:
            async with AsyncTcpClient(api.host, api.port) as client:
                await client.write_event(Transcribe(language=metadata.language).event())
                await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS).event())

                # Задача для чтения ответа от сервера
                reader_task = asyncio.create_task(self._read_transcript_from_client(client, api))
                # Задача для отправки аудио на сервер
                writer_task = asyncio.create_task(self._send_audio_to_client(client, stream))

                # Ждем, пока завершится ЛЮБАЯ из задач
                done, pending = await asyncio.wait(
                    {reader_task, writer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if reader_task in done:
                    # Сервер прислал ответ ПЕРВЫМ. Это наш случай!
                    text_result = reader_task.result()
                    _LOGGER.debug("Received transcript from %s before stream ended. Stopping audio send.", api.host)
                    # Отмена задачи отправки аудио прервет цикл 'async for' и остановит VAD
                elif writer_task in done:
                    # Поток аудио закончился ПЕРВЫМ (пользователь замолчал)
                    _LOGGER.debug("Audio stream to %s ended. Waiting for final transcript.", api.host)
                    await client.write_event(AudioStop().event())
                    # Теперь дожидаемся ответа от сервера
                    text_result = await asyncio.wait_for(reader_task, timeout=9.0)

        except Exception as e:
            _LOGGER.error("Exception in concurrent transcription with %s: %s", api.host, e)
            raise  # Передаем исключение выше для обработки
        finally:
            # Важно: отменяем все еще работающие задачи, чтобы избежать "висячих" процессов
            if writer_task and not writer_task.done():
                writer_task.cancel()
            if reader_task and not reader_task.done():
                reader_task.cancel()
        
        return text_result

    async def _read_transcript_from_client(self, client: AsyncTcpClient, api: SttApi) -> Optional[str]:
        """Helper coroutine to read events until a Transcript is found."""
        while True:
            event = await client.read_event()
            if event is None:
                _LOGGER.debug("Connection to %s closed by server.", api.host)
                return None
            if Error.is_type(event.type):
                _LOGGER.warning("Server %s returned an error: %s", api.host, Error.from_event(event).text)
                return None
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text

    async def _send_audio_to_client(self, client: AsyncTcpClient, stream: AsyncIterable[bytes]) -> None:
        """Helper coroutine to stream audio chunks to the server."""
        async for audio_bytes in stream:
            chunk = AudioChunk(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS, audio=audio_bytes)
            await client.write_event(chunk.event())

    async def _try_transcribe(self, api: SttApi, metadata: SpeechMetadata, audio_chunks: List[bytes]) -> Optional[str]:
        """Helper to send buffered audio chunks to a specific server."""
        try:
            async with AsyncTcpClient(api.host, api.port) as client:
                await client.write_event(Transcribe(language=metadata.language).event())
                await client.write_event(AudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS).event())
                
                for audio_bytes in audio_chunks:
                    await client.write_event(AudioChunk(rate=SAMPLE_RATE, width=SAMPLE_WIDTH, channels=SAMPLE_CHANNELS, audio=audio_bytes).event())
                
                await client.write_event(AudioStop().event())

                while True:
                    event = await client.read_event()
                    if event is None: return None
                    if Error.is_type(event.type):
                        _LOGGER.warning("Server %s returned an error: %s", api.host, Error.from_event(event).text)
                        return None
                    if Transcript.is_type(event.type):
                        return Transcript.from_event(event).text
            return None
        except Exception:
            raise