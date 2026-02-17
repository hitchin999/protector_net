[![Total Downloads](https://img.shields.io/github/downloads/hitchin999/protector_net/total.svg?label=Total%20Downloads&style=for-the-badge&color=blue)](https://github.com/hitchin999/protector_net/releases)
[![Active Protector.Net Installs][prot-badge]][prot-analytics]

[prot-badge]: https://img.shields.io/badge/dynamic/json?label=Active%20Installs&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.protector_net.total&style=for-the-badge&color=blue
[prot-analytics]: https://analytics.home-assistant.io/integration/protector_net

# Protector.Net & Odyssey Access Control for Home Assistant

This custom integration controls **Hartmann Controls Protector.Net _and_ Odyssey** door access systems via HTTP + a live **SignalR** websocket for instant updates.

---

## What’s new in 0.2.2

### Temporary Access Codes
Create and manage temporary PIN codes for doors — perfect for Airbnb, rental properties, or visitor management.
- `create_temp_code` — Generate random or manual PINs with optional start/end times
- `update_temp_code` — Extend a guest’s stay by patching the expiration directly on Hartmann. Same PIN, no downtime, no interruption to the guest.
- `delete_temp_code` / `delete_temp_code_by_name` — Remove codes by PIN or name
- `clear_all_temp_codes` — Wipe all temp codes from a door
- **Temp Code sensor** per door stores `code_name`, `code`, `user_id`, `start_time`, and `end_time` per code for automation-driven extension detection
- Configurable default PIN digits (4–9) in integration setup and options

### OTR Schedules (OneTimeRun)
Schedule future door overrides that execute directly in the Hartmann panel — works even if Home Assistant is offline.
- `create_otr_schedule` / `delete_otr_schedule` / `get_otr_schedules`
- All times automatically converted between your local timezone and Hartmann’s UTC
- **OTR Schedules sensor** per door — state is the schedule count, attributes include `active_schedules`, `upcoming_schedules`, and `all_schedules` with id, name, mode, start, and stop times
- Refreshes every 5 minutes + immediately after create/delete via dispatcher signals

### Override Until (date/time picker)
Pick a target date & time for timed overrides instead of calculating minutes manually — just like Hartmann’s app.
- **Override Until** datetime entity per door — when Override Type is “For Specified Time”, the Override switch auto-computes minutes from the datetime picker
- Falls back to Override Minutes if the datetime is empty or in the past
- **`until` parameter on `override_door` service** — pass `until: "2026-02-18T14:00:00"` and the service auto-computes minutes. Great for automations where you know the end time, not the duration.

### Instant Door Override
- `override_door` — Single service call to override a door (no need to set mode + enable separately)
- `resume_door` — Resume normal schedule
- Override Type and Override Mode selects update immediately in the UI after service calls

### Update Panels
- **Button** on the Hub device + `update_panels` **service** to push configuration to all connected panels immediately, so changes take effect without waiting for Hartmann’s auto-sync interval

### Last Door Log Improvements
- State now includes timestamp: “John Smith granted access @ 1:06 PM” — each event is a separate History entry
- OTR schedule activations appear as “OTR Unlock @ 10:01 PM”

### Other Changes
- **Multi-door support** — All services accept multiple doors via the device picker
- **Override Minutes** max increased to 2,147,483,647
- `force_remove` option on `delete_temp_code_by_name` for cleaning stale sensor entries
- Better error messages when PIN creation fails (shows Hartmann’s actual error)

---

## Features

* ✅ Cookie login (`ss-id`) with automatic re-auth
* ✅ Partition selection (imports only your chosen partition)
* ✅ **Zero-polling** live updates via SignalR
* ✅ Door controls: per-door override UI + Pulse Unlock (+ optional legacy buttons)
* ✅ **Override Until** date/time picker for timed overrides
* ✅ **All Doors Lockdown** switch (partition-wide)
* ✅ **Temporary access codes** with start/end times, extension support, and auto-cleanup
* ✅ **OTR Schedules** — schedule overrides that run on the panel even if HA is offline
* ✅ **HA Door Log** entries when you use HA buttons (e.g., “Home Assistant unlocked …”)
* ✅ All controls & options in the UI (HACS-friendly)
* ✅ **Odyssey servers supported** (auto-detect)
  
---

## Installation

**HACS (recommended):**

1. In **HACS → Integrations**, search for **“Protector.Net Access Control”** and install.
2. **Restart** Home Assistant.

**Manual:**

1. Copy `custom_components/protector_net/` into your Home Assistant `config/custom_components/`.
2. **Restart** Home Assistant.

Then go to **Settings → Devices & Services → Add Integration** → “Protector.Net Access Control”.

---

## Setup & Options

* **Base URL** – `https://host:port`
* **Credentials** – a Protector.Net user with sufficient privileges (**must be a System Administrator** in Hartmann)
* **Default override minutes** – used for Timed Override
* **Default PIN digits** – 4–9 digits for generated temp codes
* **Partition** – select exactly one
* **Action Plans** – pick trigger plans to clone as **System** plans (so they can be executed from HA)

Revisit any time: **Settings → Devices & Services → Protector.Net → Options**.

### Door Entities (legacy buttons)

* **Pulse Unlock** is always included (not shown in the picker).
* Choose any **additional** legacy buttons you want; **none** are pre-selected by default.

---

## Devices & Entities

### 1) Hub device (per partition)

* **Device:** `Hub Status – <Partition>`
* **Entity:** **Hub Status – <Partition>** *(sensor)*
  **State:** `running / connecting / idle / stopped / error`
  **Attributes:** `phase`, `connected`, `mapped_doors`, `partition_id`, `system_type` *(“Odyssey” or “ProtectorNET”)* 
* **Update Panels** *(button)* — push configuration to all connected panels immediately

### 2) Door devices (one per door)

**Sensors**

* **Lock State** — `Locked` / `Unlocked`
* **Overridden** — `On` / `Off`
* **Reader Mode** — mapped from controller index:
  `0/8 Lockdown, 1 Card, 2 Pin, 3 Card or Pin, 4 Card and Pin, 5 Unlock, 6 First Credential In, 7 Dual Credential`
* **Last Door Log by** — highlights the last actor with timestamp (e.g., “John Smith granted access @ 1:06 PM”); attributes include the last **reader/action** message/time and the last **door** message.
* **Temp Code** — state is `None` or the active code name. Attributes: list of all temp codes with `code_name`, `code`, `user_id`, `start_time`, `end_time` per entry.
* **OTR Schedules** — state is the count of schedules for this door. Attributes: `active_schedules` (currently running), `upcoming_schedules` (future), and `all_schedules` with id, name, mode, start, and stop times. Refreshes every 5 minutes and immediately after create/delete.

**Controls**

* **Override** *(switch)* — ON applies selected **Type** + **Mode** (and minutes if “For Specified Time”); OFF resumes schedule and forces **Override Mode = None**.
* **Override Type** *(select)* — `For Specified Time` / `Until Resumed` / `Until Next Schedule`
* **Override Mode** *(select)* — `None`, `Card`, `Pin`, `Unlock`, `Card and Pin`, `Card or Pin`, `First Credential In`, `Dual Credential`, `Lockdown`
  * OFF ⇒ shows **`None`**; ON ⇒ mirrors the panel’s current reader mode.
* **Override Minutes** *(number)* — used when type is “For Specified Time” (fallback if Override Until is not set)
* **Override Until** *(datetime)* — pick a target date & time; the switch auto-computes minutes at turn-on. Overrides the Minutes field when set to a future time.

**Door Buttons**

* **Always:** Pulse Unlock
* **Optional (if selected in Options):** Resume Schedule, Unlock Until Resume, Unlock Until Next Schedule, CardOrPin Until Resume, Timed Override Unlock

### 3) Action Plans device (per partition)

* **Device:** `Action Plans – <Partition>`
* **Entities:** `Action Plan: <Plan Name>` *(button)* — executes cloned System-type plans.

### 4) All Doors device (per partition)

* **Device:** `All Doors – <Partition>`
* **Entity:** **All Doors Lockdown** *(switch)*
  **ON:** apply **Lockdown** override on **all doors** in the partition.
  **OFF:** **Resume Schedule** across all doors.

---

## Services

### Temporary Access Codes

| Service | Description |
|---------|-------------|
| `create_temp_code` | Create a temporary PIN code with optional start/end times. Supports random or manual codes, configurable digit count (4–9). |
| `update_temp_code` | Update start/end time of an existing code without changing the PIN. Perfect for extending guest stays. |
| `delete_temp_code` | Delete a temp code by PIN value. |
| `delete_temp_code_by_name` | Delete a temp code by name (for calendar automations). Optional `force_remove` to clean stale sensor entries. |
| `clear_all_temp_codes` | Remove all temporary codes from a door. |

### OTR Schedules

| Service | Description |
|---------|-------------|
| `create_otr_schedule` | Schedule a future door override with start/stop times and mode. Stored on the Hartmann panel — runs even if HA is offline. |
| `delete_otr_schedule` | Delete OTR schedules by door (all) or specific schedule ID. |
| `get_otr_schedules` | Retrieve all OTR schedules. |

### Door Override & Control

| Service | Description |
|---------|-------------|
| `override_door` | Apply an override to door(s) in a single call. Supports `mode`, `override_type`, `minutes`, and `until` (datetime — auto-computes minutes). |
| `resume_door` | Resume normal schedule for door(s). |
| `update_panels` | Push current configuration to all connected panels immediately. |

All services accept **multiple doors** via the device picker.

---

## Booking Automation Example

A complete three-phase automation for calendar-based booking management. Handles new bookings, stay extensions, and cleanup:

```yaml
alias: "Booking Code Manager"
description: "Create/extend/cleanup temp codes from calendar bookings"
triggers:
  # Phase 1 & 2: Calendar event starts or changes
  - trigger: calendar
    entity_id: calendar.your_booking_calendar
    event: start
    offset: "-00:30:00"  # 30 min before check-in
  # Phase 3: Cleanup 24h after checkout
  - trigger: calendar
    entity_id: calendar.your_booking_calendar
    event: end
    offset: "24:00:00"

actions:
  - choose:
      # --- Phase 3: Cleanup ---
      - conditions:
          - condition: template
            value_template: "{{ trigger.event == 'end' }}"
        sequence:
          - action: protector_net.delete_temp_code_by_name
            data:
              door_device_id: "YOUR_DOOR_DEVICE_ID"
              code_name: "{{ trigger.calendar_event.summary }}"
              force_remove: true

      # --- Phase 1 & 2: Create or Extend ---
      - conditions:
          - condition: template
            value_template: "{{ trigger.event == 'start' }}"
        sequence:
          - variables:
              booking_name: "{{ trigger.calendar_event.summary }}"
              new_end: "{{ trigger.calendar_event.end }}"
              # Check if code already exists (for extension detection)
              existing_codes: >-
                {{ state_attr('sensor.your_door_temp_code', 'codes') or [] }}
              existing_code: >-
                {{ existing_codes | selectattr('code_name', 'eq', booking_name)
                   | list | first | default(none) }}
          - choose:
              # Extension: code exists but end time changed
              - conditions:
                  - condition: template
                    value_template: >-
                      {{ existing_code is not none and
                         existing_code.end_time != new_end }}
                sequence:
                  - action: protector_net.update_temp_code
                    data:
                      door_device_id: "YOUR_DOOR_DEVICE_ID"
                      code_name: "{{ booking_name }}"
                      end_time: "{{ new_end }}"
                  - action: protector_net.update_panels

              # New booking: no existing code
              - conditions:
                  - condition: template
                    value_template: "{{ existing_code is none }}"
                sequence:
                  - action: protector_net.create_temp_code
                    data:
                      door_device_id: "YOUR_DOOR_DEVICE_ID"
                      code_name: "{{ booking_name }}"
                      random_code: true
                      start_time: "{{ trigger.calendar_event.start }}"
                      end_time: "{{ new_end }}"
                    response_variable: result
                  - action: protector_net.update_panels
                  - action: notify.your_notification_service
                    data:
                      message: >-
                        New code for {{ booking_name }}: {{ result.code }}
                        (valid until {{ new_end }})
```

---

## How “Last Door Log by” works

* **State** becomes the **person/app** with timestamp when:
  * Access granted/denied events (e.g., “John Smith granted access @ 1:06 PM”)
  * Action plan messages like “Home Assistant unlocked …”
  * OTR activations (e.g., “OTR Unlock @ 10:01 PM”)
* **Attributes** are stable and minimal:
  * `Reader Message`, `Reader Message Time` (granted/denied or action text + timestamp)
  * `Door Message` (e.g., “Door is now Locked/Unlocked”)
  * `Door ID`, `Partition ID`

Lock/Unlock **status** messages don’t flip the “by” state (that’s what **Lock State** is for).

---

## Troubleshooting

* **Sensors didn’t appear previously for “Default Partition”**
  Update to **0.1.7**; discovery now correctly loads those doors.

---

## Changelog

### 0.2.2
* New: **Temporary access codes** — `create_temp_code`, `update_temp_code`, `delete_temp_code`, `delete_temp_code_by_name`, `clear_all_temp_codes`
* New: **Temp Code sensor** per door with code details in attributes
* New: **OTR Schedules** — `create_otr_schedule`, `delete_otr_schedule`, `get_otr_schedules`
* New: **OTR Schedules sensor** per door with active/upcoming schedule breakdown
* New: **Override Until** datetime entity per door — pick a target date & time instead of calculating minutes
* New: **`until` parameter** on `override_door` service — auto-computes minutes from a target datetime
* New: **`override_door`** and **`resume_door`** services for single-call door control
* New: **Update Panels** button and service
* New: **Last Door Log** now includes timestamps and OTR events
* New: **Multi-door support** — all services accept multiple doors
* New: **Override Minutes** max increased to 2,147,483,647
* New: `force_remove` option on `delete_temp_code_by_name`
* New: Configurable default PIN digits (4–9)

### 0.2.1
* New: **Odyssey servers supported** (auto-detect, no config changes).
* New: **Odyssey status snapshots** on connect and periodically (~60s) to catch schedule flips.
* Improvement: Normalize WS types for `overridden` and `timeZone` from Odyssey.

### 0.2.0
* Fix: **Last Door Log not updating for some doors** – fixed a bug where notifications coming from a *reader* (instead of directly from the door) were being dropped because the door ID wasn’t in the partition allowlist yet.
* Note: **0.1.9 withdrawn**.

### 0.1.8
* Fix: Reader notifications (including “Reader 2” / in–out readers on the same ODM/TDM) now map cleanly to the right **door** because we also pull the partition-scoped **AvailableReaders** API (Reader → DoorId), not just the name.

### 0.1.7
* Fix: Door sensors now load correctly when the selected partition is named **“Default Partition.”**
* Reliability: More robust, partition-scoped discovery with safe fallback and retry.

### 0.1.6
* New: **Partition-scoped devices** (Hub + Action Plans + **All Doors**)
* New: **All Doors Lockdown** switch (partition-wide)
* New: **5 sensors** total (Hub Status, Lock State, Overridden, Reader Mode, **Last Door Log by**)
* New: **Per-door Override UI** (Switch + Selects + Number), with **instant sync**
* Change: **Legacy buttons** refined — **Pulse Unlock always**; others **optional** via Options

### 0.1.5
* Door-action logs (“Home Assistant unlocked …”)

### 0.1.4
* Plan cloning fixes and reuse; HA Door Log plan

### 0.1.3
* Action Plans import/execute; options refresh

### 0.1.2
* Configurable door entities; options flow; base fixes

### 0.1.1
* Partition selection; session refresh; dynamic titles

### 0.1.0
* Initial release (doors & basic controls)

---

**Author:** Yoel Goldstein / Vaayer LLC
