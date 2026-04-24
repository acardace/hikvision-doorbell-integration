"""The Hikvision Doorbell Two-Way Audio integration."""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_AUDIO_FILE,
    DOMAIN,
    SERVICE_ABORT,
    SERVICE_PLAY_FILE,
)
from .coordinator import HikvisionDoorbellCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CAMERA]

SERVICE_PLAY_FILE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required(ATTR_AUDIO_FILE): cv.string,
    }
)

SERVICE_ABORT_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hikvision Doorbell from a config entry."""
    server_url = entry.data[CONF_HOST]

    coordinator = HikvisionDoorbellCoordinator(hass, server_url)

    # Test connection to server
    try:
        await coordinator.async_test_connection()
    except Exception as err:
        _LOGGER.error("Failed to connect to server: %s", err)
        raise ConfigEntryNotReady(f"Cannot connect to server at {server_url}") from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register Lovelace resource for custom card
    await _register_lovelace_resource(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register service
    async def handle_play_file(call: ServiceCall) -> None:
        """Handle play_file service call.

        Sends audio file to the doorbell speaker. If the file is not in WAV format,
        it will be converted to G.711 µ-law using ffmpeg.
        The server handles session management automatically.
        """
        coordinator = _get_coordinator_from_entity(hass, call.data["entity_id"])
        audio_file = call.data[ATTR_AUDIO_FILE]

        # Ensure file exists and is readable
        if not os.path.isfile(audio_file):
            raise HomeAssistantError(f"Audio file not found: {audio_file}")

        # Check if file is already in WAV format
        is_wav = audio_file.lower().endswith(".wav")

        if is_wav:
            # Use the file directly without conversion
            _LOGGER.debug("File is already in WAV format, skipping conversion")
            if not await coordinator.async_send_audio_file(audio_file):
                raise HomeAssistantError("Failed to send audio file")
        else:
            # Convert audio file to G.711 µ-law using ffmpeg
            converted_file = None
            try:
                converted_file = await _convert_audio_to_ulaw(hass, audio_file)

                # Send converted audio file
                if not await coordinator.async_send_audio_file(converted_file):
                    raise HomeAssistantError("Failed to send audio file")
            finally:
                # Clean up temporary converted file
                if converted_file and os.path.exists(converted_file):
                    try:
                        os.unlink(converted_file)
                    except OSError as err:
                        _LOGGER.warning("Failed to delete temporary file %s: %s", converted_file, err)

    async def handle_abort(call: ServiceCall) -> None:
        """Handle abort service call.

        Aborts all active operations (play-file and WebRTC sessions)
        and releases all audio channels on the doorbell.
        """
        coordinator = _get_coordinator_from_entity(hass, call.data["entity_id"])

        if not await coordinator.async_abort_operations():
            raise HomeAssistantError("Failed to abort operations")

    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_FILE, handle_play_file, schema=SERVICE_PLAY_FILE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ABORT, handle_abort, schema=SERVICE_ABORT_SCHEMA
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    # Unregister services if this is the last entry
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_PLAY_FILE)
        hass.services.async_remove(DOMAIN, SERVICE_ABORT)

    return unload_ok


def _get_coordinator_from_entity(hass: HomeAssistant, entity_id: str) -> HikvisionDoorbellCoordinator:
    """Get coordinator from entity_id."""
    # Extract entry_id from entity registry
    entity_reg = er.async_get(hass)
    entity_entry = entity_reg.async_get(entity_id)

    if not entity_entry:
        raise HomeAssistantError(f"Entity not found: {entity_id}")

    return hass.data[DOMAIN][entity_entry.config_entry_id]


async def _convert_audio_to_ulaw(hass: HomeAssistant, input_file: str) -> str:
    """Convert audio file to G.711 µ-law format using ffmpeg.

    Returns path to the converted temporary file.
    """
    # Create temporary file for converted audio
    temp_fd, temp_path = tempfile.mkstemp(suffix=".wav", prefix="doorbell_")
    os.close(temp_fd)

    try:
        # ffmpeg command to convert to G.711 µ-law
        # -acodec pcm_mulaw: G.711 µ-law codec
        # -ar 8000: 8kHz sample rate (required for G.711)
        # -ac 1: mono audio
        # -f wav: WAV container format
        ffmpeg_cmd = [
            "ffmpeg",
            "-i", input_file,
            "-acodec", "pcm_mulaw",
            "-ar", "8000",
            "-ac", "1",
            "-f", "wav",
            "-y",  # Overwrite output file
            temp_path
        ]

        _LOGGER.debug("Running ffmpeg: %s", " ".join(ffmpeg_cmd))

        # Run ffmpeg
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            _LOGGER.error("ffmpeg conversion failed: %s", error_msg)
            raise HomeAssistantError(f"Audio conversion failed: {error_msg}")

        _LOGGER.debug("Audio file converted successfully to %s", temp_path)
        return temp_path

    except Exception as err:
        # Clean up temp file on error
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise HomeAssistantError(f"Failed to convert audio: {err}") from err


async def _register_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the custom Lovelace card resource automatically.

    If the card JS is bundled in the integration's www folder (e.g., via addon),
    it will be automatically registered. Otherwise, users need to add it manually.
    """
    card_path = os.path.join(os.path.dirname(__file__), "www", "hikvision-doorbell-card.js")
    card_url = "/hikvision_doorbell/hikvision-doorbell-card.js"

    if not os.path.isfile(card_path):
        _LOGGER.debug("Card JS not found at %s, skipping auto-registration", card_path)
        return

    # Register static path for the card
    await hass.http.async_register_static_paths([StaticPathConfig(card_url, card_path, True)])
    _LOGGER.info("Registered Hikvision Doorbell card at %s", card_url)

    # Add to Lovelace resources if not already present
    try:
        resources = await hass.components.lovelace.async_get_resources()
        resource_urls = [r.get("url", "") for r in resources]

        if card_url not in resource_urls:
            await hass.components.lovelace.async_create_resource({
                "url": card_url,
                "res_type": "module"
            })
            _LOGGER.info("Added Hikvision Doorbell card to Lovelace resources")
        else:
            _LOGGER.debug("Hikvision Doorbell card already in Lovelace resources")
    except Exception as err:
        _LOGGER.warning(
            "Could not auto-add card to Lovelace resources: %s. "
            "Please add manually: URL=%s, Type=JavaScript Module",
            err, card_url
        )
