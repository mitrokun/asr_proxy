import asyncio
import logging
from typing import Optional

from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

class CannotConnect(HomeAssistantError):
    """Error indicating inability to connect."""

class SttApi:
    """Class for interacting with a Wyoming STT server."""

    def __init__(self, host: str, port: int):
        """Initialize the API client."""
        self.host = host
        self.port = port
        self.info: Optional[Info] = None

    async def connect_and_get_info(self) -> Info:
        """
        Connects to the server, requests and returns information about it.
        Caches the result.
        """
        if self.info:
            return self.info
            
        _LOGGER.debug("Requesting information from server %s:%s", self.host, self.port)
        try:
            async with AsyncTcpClient(self.host, self.port) as client:
                await client.write_event(Describe().event())
                event = await asyncio.wait_for(client.read_event(), timeout=5)

                if event is None or not Info.is_type(event.type):
                    raise CannotConnect(f"Server {self.host}:{self.port} did not return valid Info data.")

                self.info = Info.from_event(event)
                _LOGGER.debug("Received information from %s:%s. ASR: %s", self.host, self.port, bool(self.info.asr))
                if not self.info.asr:
                    raise CannotConnect(f"Server {self.host}:{self.port} does not provide ASR/STT services.")

                return self.info

        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as err:
            _LOGGER.error("Failed to connect to %s:%s: %s", self.host, self.port, err)
            raise CannotConnect(f"Failed to connect to {self.host}:{self.port}") from err