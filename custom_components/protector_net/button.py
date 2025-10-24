from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
import re

from . import api
from .const import DEFAULT_OVERRIDE_MINUTES, KEY_PLAN_IDS, DOMAIN
from .device import ProtectorNetDevice

_LOGGER = logging.getLogger(__name__)

# Legacy door button keys (must match config_flow)
LEGACY_PULSE = "_pulse_unlock"
LEGACY_KEYS_OPTIONAL = {
    "_resume_schedule",
    "_unlock_until_resume",
    "_override_card_or_pin",
    "_unlock_until_next_schedule",
    "_timed_override_unlock",
}

def _selected_legacy(entry) -> set[str]:
    """
    Return the set of legacy door buttons to expose.
    Always includes Pulse Unlock. Optional picks come from entry.options/entities (or data fallback).
    """
    raw = entry.options.get("entities", entry.data.get("entities"))
    if not raw:
        return {LEGACY_PULSE}
    if not isinstance(raw, list):
        raw = [raw]
    selected = {str(x).strip() for x in raw if x}
    selected.add(LEGACY_PULSE)  # ensure Pulse Unlock is always present
    # Only allow known keys
    return {k for k in selected if (k == LEGACY_PULSE or k in LEGACY_KEYS_OPTIONAL)}

def _migrate_button_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Migrate v1.0.5 button unique_ids -> v0.1.6 format (inject entry_id).
    Old (1.0.5):
      protector_net_{host}_{door_id}_{suffix}
      protector_net_{host}_action_plan_{plan_id}
    New (0.1.6):
      protector_net_{host}_{entry_id}_{door_id}_{suffix}
      protector_net_{host}_{entry_id}_action_plan_{plan_id}
    """
    registry = er.async_get(hass)
    host = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")
    eid = entry.entry_id

    door_suffixes = (
        "pulse_unlock",
        "resume_schedule",
        "unlock_until_resume",
        "cardorpin_until_resume",
        "unlock_until_next_schedule",
        "timed_override_unlock",
    )

    door_re = re.compile(
        rf"^protector_net_{re.escape(host)}_(?P<door>\d+)_(?P<suf>{'|'.join(door_suffixes)})$"
    )
    plan_re = re.compile(
        rf"^protector_net_{re.escape(host)}_action_plan_(?P<pid>\d+)$"
    )
    already_new_re = re.compile(
        rf"^protector_net_{re.escape(host)}_{re.escape(eid)}_"
    )

    for entity in list(registry.entities.values()):
        if entity.config_entry_id != eid:
            continue
        # integration platform must match our integration; entity domain must be 'button'
        if entity.platform != DOMAIN or entity.domain != "button":
            continue

        uid = entity.unique_id or ""
        if already_new_re.match(uid):
            continue  # nothing to do

        m = door_re.match(uid)
        if m:
            new_uid = f"protector_net_{host}_{eid}_{m.group('door')}_{m.group('suf')}"
            if new_uid != uid:
                try:
                    registry.async_update_entity(entity.entity_id, new_unique_id=new_uid)
                    _LOGGER.debug("[%s] migrated button unique_id: %s -> %s", eid, uid, new_uid)
                except ValueError:
                    _LOGGER.warning(
                        "[%s] unique_id %s already exists; leaving %s as-is",
                        eid, new_uid, entity.entity_id
                    )
            continue

        m = plan_re.match(uid)
        if m:
            new_uid = f"protector_net_{host}_{eid}_action_plan_{m.group('pid')}"
            if new_uid != uid:
                try:
                    registry.async_update_entity(entity.entity_id, new_unique_id=new_uid)
                    _LOGGER.debug("[%s] migrated plan unique_id: %s -> %s", eid, uid, new_uid)
                except ValueError:
                    _LOGGER.warning(
                        "[%s] unique_id %s already exists; leaving %s as-is",
                        eid, new_uid, entity.entity_id
                    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up Protector.Net door and action-plan buttons.

    Legacy door buttons are created based on the selection saved in config/options:
      - Pulse Unlock is always included
      - Other legacy buttons are only added if selected
    Network work happens in a background task to avoid blocking HA startup.
    """
    _migrate_button_unique_ids(hass, entry)
    
    host_safe = (urlparse(entry.data["base_url"]).hostname or "").replace(":", "_")

    async def _setup_buttons_later() -> None:
        # Gather remote data with timeouts
        try:
            await asyncio.sleep(0.5)
            doors = await asyncio.wait_for(api.get_all_doors(hass, entry.entry_id), timeout=30)
        except asyncio.TimeoutError:
            _LOGGER.error("[%s] get_all_doors timed out; no door buttons will be created right now", entry.entry_id)
            return
        except Exception as e:
            _LOGGER.error("[%s] Failed to fetch doors from Protector.Net: %s", entry.entry_id, e)
            return

        # Provision HA log plan (best-effort)
        try:
            log_plan_id = await asyncio.wait_for(api.find_or_create_ha_log_plan(hass, entry.entry_id), timeout=30)
            hass.data[DOMAIN][entry.entry_id]["ha_log_plan_id"] = log_plan_id
            _LOGGER.debug("[%s] HA log plan id is %s", entry.entry_id, log_plan_id)
        except Exception as e:
            _LOGGER.error("[%s] Failed to create/find HA log plan: %s", entry.entry_id, e)
            hass.data[DOMAIN][entry.entry_id]["ha_log_plan_id"] = None

        # Default minutes (options override data; both fall back to const)
        override_mins = entry.options.get(
            "override_minutes",
            entry.data.get("override_minutes", DEFAULT_OVERRIDE_MINUTES),
        )

        # Selected trigger plans to clone to system plans
        raw_plan_ids = entry.options.get(KEY_PLAN_IDS, entry.data.get(KEY_PLAN_IDS, []))
        plan_ids: list[int] = []
        for pid in raw_plan_ids or []:
            try:
                plan_ids.append(int(pid))
            except Exception:
                continue

        # Clone/resolve system plans
        system_ids: list[int] = []
        for trig_id in plan_ids:
            try:
                sys_id = await asyncio.wait_for(api.find_or_clone_system_plan(hass, entry.entry_id, trig_id), timeout=30)
                system_ids.append(sys_id)
            except asyncio.TimeoutError:
                _LOGGER.error("[%s] find_or_clone_system_plan(%s) timed out", entry.entry_id, trig_id)
            except Exception as e:
                _LOGGER.error("[%s] Error cloning trigger plan %s: %s", entry.entry_id, trig_id, e)
        _LOGGER.debug("[%s] System plan ids=%s", entry.entry_id, system_ids)

        # ---------- Only add selected legacy door buttons (Pulse Unlock always) ----------
        selected = _selected_legacy(entry)

        entities: list[ButtonEntity] = []

        for door in doors:
            if LEGACY_PULSE in selected:
                entities.append(DoorPulseUnlockButton(hass, entry, door, host_safe))
            if "_resume_schedule" in selected:
                entities.append(DoorResumeScheduleButton(hass, entry, door, host_safe))
            if "_unlock_until_resume" in selected:
                entities.append(DoorOverrideUntilResumeButton(hass, entry, door, host_safe))
            if "_override_card_or_pin" in selected:
                entities.append(DoorOverrideUntilResumeCardOrPinButton(hass, entry, door, host_safe))
            if "_unlock_until_next_schedule" in selected:
                entities.append(DoorOverrideUntilNextScheduleButton(hass, entry, door, host_safe))
            if "_timed_override_unlock" in selected:
                entities.append(DoorTimedOverrideUnlockButton(hass, entry, door, host_safe, override_mins))

        # ---------- Action Plan buttons (System clones) ----------
        if system_ids:
            try:
                plans = await asyncio.wait_for(api.get_action_plans(hass, entry.entry_id), timeout=30)
            except asyncio.TimeoutError:
                _LOGGER.error("[%s] get_action_plans timed out; skipping action plan buttons", entry.entry_id)
                plans = []
            except Exception as e:
                _LOGGER.error("[%s] Failed to fetch action plans: %s", entry.entry_id, e)
                plans = []

            for plan in plans:
                if plan.get("Id") in system_ids:
                    entities.append(ActionPlanButton(hass, entry, plan, host_safe))

        if entities:
            async_add_entities(entities)
            _LOGGER.debug("[%s] Added %d button entities", entry.entry_id, len(entities))
        else:
            _LOGGER.debug("[%s] No button entities to add", entry.entry_id)

    hass.async_create_task(_setup_buttons_later())


# -----------------------
# Door-level buttons
# -----------------------
class BaseDoorButton(ProtectorNetDevice, ButtonEntity):
    """Base class for door buttons—handles per-door device_info & entry scoping."""
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, door: dict, host_safe: str):
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._door = door
        self.door_id = door["Id"]
        self.door_name = door.get("Name", "Unknown Door")

        entry_data = hass.data[DOMAIN][entry.entry_id]
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")
        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")
        self._host_safe = host_safe

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"door:{self._host_key}:{self.door_id}|{self._entry_id}")},
            "name": self.door_name,
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Door",
            "via_device": (DOMAIN, self._hub_identifier),
            "configuration_url": self._entry.data.get("base_url"),
        }


class DoorPulseUnlockButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Pulse Unlock"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_pulse_unlock"

    async def async_press(self):
        await api.pulse_unlock(self.hass, self._entry_id, [self.door_id])
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if plan_id:
            await api.execute_action_plan(
                self.hass, self._entry_id, plan_id,
                variables={"App": "Home Assistant", "Door": self.door_name},
            )


class DoorResumeScheduleButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Resume Schedule"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_resume_schedule"

    async def async_press(self):
        await api.resume_schedule(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilResumeButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Unlock Until Resume"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_unlock_until_resume"

    async def async_press(self):
        await api.set_override(self.hass, self._entry_id, [self.door_id], "Resume")
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if plan_id:
            await api.execute_action_plan(
                self.hass, self._entry_id, plan_id,
                variables={"App": "Home Assistant", "Door": self.door_name},
            )


class DoorOverrideUntilResumeCardOrPinButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "CardOrPin Until Resume"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_cardorpin_until_resume"

    async def async_press(self):
        await api.override_until_resume_card_or_pin(self.hass, self._entry_id, [self.door_id])


class DoorOverrideUntilNextScheduleButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str):
        super().__init__(hass, entry, door, host_safe)
        self._attr_name = "Unlock Until Next Schedule"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_unlock_until_next_schedule"

    async def async_press(self):
        await api.set_override(self.hass, self._entry_id, [self.door_id], "Schedule")
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if plan_id:
            await api.execute_action_plan(
                self.hass, self._entry_id, plan_id,
                variables={"App": "Home Assistant", "Door": self.door_name},
            )


class DoorTimedOverrideUnlockButton(BaseDoorButton):
    def __init__(self, hass, entry, door, host_safe: str, minutes: int):
        super().__init__(hass, entry, door, host_safe)
        self._override_minutes = minutes
        self._attr_name = "Timed Override Unlock"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_{self.door_id}_timed_override_unlock"

    async def async_press(self):
        await api.set_override(self.hass, self._entry_id, [self.door_id], "Time", minutes=self._override_minutes)
        plan_id = self.hass.data[DOMAIN][self._entry_id].get("ha_log_plan_id")
        if plan_id:
            await api.execute_action_plan(
                self.hass, self._entry_id, plan_id,
                variables={"App": "Home Assistant", "Door": self.door_name},
            )


# -----------------------
# Hub-level (Action Plan) buttons
# -----------------------
class ActionPlanButton(ProtectorNetDevice, ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, plan: dict, host_safe: str):
        super().__init__(entry)
        self.hass = hass
        self._entry = entry
        self._entry_id = entry.entry_id
        self._plan = plan
        self._host_safe = host_safe

        entry_data = hass.data[DOMAIN][entry.entry_id]
        self._host_key: str = entry_data.get("host") or (urlparse(entry.data["base_url"]).netloc or "")

        if entry_data.get("partition_name"):
            self._partition_name: str = entry_data["partition_name"]
        elif entry.title and "–" in entry.title:
            self._partition_name = entry.title.split("–", 1)[1].strip()
        else:
            self._partition_name = str(entry_data.get("partition_id"))

        self._hub_identifier: str = entry_data.get("hub_identifier", f"hub:{self._host_key}|{self._entry_id}")

        self._attr_name = f"Action Plan: {plan['Name']}"
        self._attr_unique_id = f"protector_net_{self._host_safe}_{self._entry_id}_action_plan_{plan['Id']}"

    @property
    def device_info(self):
        device_ident = f"actionplans:{self._host_key}|{self._entry_id}"
        info = {
            "identifiers": {(DOMAIN, device_ident)},
            "manufacturer": "Yoel Goldstein/Vaayer LLC",
            "model": "Protector.Net Action Plans",
            "name": f"Action Plans – {self._partition_name}",
            "configuration_url": self._entry.data.get("base_url"),
        }
        return info

    async def async_press(self) -> None:
        success = await api.execute_action_plan(self.hass, self._entry_id, self._plan["Id"])
        if not success:
            _LOGGER.error("[%s] Failed to execute action plan %s", self._entry_id, self._plan["Id"])
