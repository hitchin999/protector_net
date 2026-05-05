[![Total Downloads](https://img.shields.io/github/downloads/hitchin999/protector_net/total.svg?label=Total%20Downloads&style=for-the-badge&color=blue)](https://github.com/hitchin999/protector_net/releases)
[![Active Protector.Net Installs][prot-badge]][prot-analytics]

[prot-badge]: https://img.shields.io/badge/dynamic/json?label=Active%20Installs&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.protector_net.total&style=for-the-badge&color=blue
[prot-analytics]: https://analytics.home-assistant.io/integration/protector_net

# Protector.Net & Odyssey Access Control for Home Assistant

This custom integration controls **Hartmann Controls Protector.Net _and_ Odyssey** door access systems via HTTP + a live **SignalR** websocket for instant updates.

---

## What's new in 0.2.5

### HA-managed door schedules (survives panel reboots)
The existing `override_door` (and the "Unlock Until" controls it powers) lives **only in the panel's RAM**. If a door panel reboots mid-override, it forgets the override and falls back to whatever base schedule the door has set in Hartmann. That made the common "Always Card or Pin in Hartmann + HA override on top" pattern unreliable for anything safety-critical or scheduled — rental check-ins, facility opening hours, scheduled lockdowns, etc.

This release adds a parallel mechanism that rewrites the door's **actual schedule** in Hartmann. Changes persist across panel reboots because they are the schedule, not an override layered on top of it.

#### How it works

A door has three states with this feature:

1. **Unmanaged** (default) — door behaves exactly as before. Untouched.
2. **Managed** — integration has created a dedicated `DoorTimeZone` in Hartmann for this door (named like `HA[abc1234] Front Door` — an HA prefix with a short id, plus the door's name) and recorded the door's *current* `DoorTimeZoneId` for rollback. The door itself still points at its original schedule. Calling `set_door_schedule_mode` updates the HA Door Time Zone but has no effect on the door yet — useful for staging the mode you want before flipping the door over.
3. **Active** — door's `DoorTimeZoneId` is flipped to the HA Door Time Zone, plus an Update Panels is sent so the panel hardware picks it up. Now the door follows the HA schedule and `set_door_schedule_mode` controls it for real.

This three-state model lets you migrate door-by-door at your own pace: provision everything first, get your automations updated to call the new service, verify the staged mode looks right, then flip doors to Active in batches once you're confident.

#### Enabling it

1. Open the integration's **Configure** panel and pick **Door Time Zones (HA-controlled schedules)** from the menu.
2. Under **Create Door Time Zones (HA-controlled schedules)**, all doors are pre-ticked. Untick the ones you DON'T want HA to manage, then tick **"Apply changes to managed doors on submit"**.
   - This last checkbox is required — it's a safety gate so opening this page can't accidentally rewrite anything.
3. *(Optional, in the same submit)* Under **Activate Door Time Zones (switch to HA schedules now)**, leave all doors pre-ticked (or untick what you want to leave on its original schedule), then tick **"Apply changes to active doors on submit"**.
4. *(Optional)* Tick **"Auto-add new doors found in Hartmann"** at the bottom if you want any future doors added in Hartmann to be automatically managed AND activated by HA on the next hourly sync.
5. Submit. The integration creates one Door Time Zone per ticked door in Hartmann (named like `HA[abc1234] Front Door`, 24/7 "Card or Pin"), and — for doors you also activated — flips their `DoorTimeZoneId` to the HA Door Time Zone and fires Update Panels.
6. Update your automations to call `protector_net.set_door_schedule_mode` instead of `override_door` for the doors you now want to control via schedule.

The two sections work independently: tick only "Apply" on Create to provision without activating (verify in Hartmann first), then come back later and tick "Apply" on Activate.

#### The new service

```yaml
service: protector_net.set_door_schedule_mode
data:
  door_device_id: "{{ device_id('button.front_door_pulse_unlock') }}"
  mode: "Unlock"        # any of: Lockdown, Card, Pin, CardOrPin,
                        #          CardAndPin, Unlock, UnlockWithFirstCardIn, DualCard
```

Behavior:

- Rewrites all 7 days of the door's HA Door Time Zone to the requested mode (12:00 AM → 11:59 PM).
- If the door is **Active**, fires Update Panels automatically so the change reaches hardware.
- If the door is only **Managed** (not yet Active), updates the HA Door Time Zone but does NOT push to panels — the new mode takes effect when you activate the door later.
- **Idempotent** — calling with the door's current mode is a no-op (no Hartmann writes, no panel push).
- Refuses doors that aren't in the managed set with a clear error pointing back to integration options.

Typical booking-style usage:

```yaml
# At check-in time
service: protector_net.set_door_schedule_mode
data:
  door_device_id: "{{ device_id('button.front_door_pulse_unlock') }}"
  mode: "Unlock"

# At check-out time
service: protector_net.set_door_schedule_mode
data:
  door_device_id: "{{ device_id('button.front_door_pulse_unlock') }}"
  mode: "CardOrPin"
```

Both calls survive a panel reboot during the booking window, because the door's actual schedule changed.

#### Auto-add new doors

Tick **"Auto-add new doors found in Hartmann"** under Door Time Zones to have new doors picked up automatically:

- The hourly background sync compares Hartmann's live door list against your saved managed-doors set
- Any door in Hartmann that's not yet managed gets a 24/7 "CardOrPin" Door Time Zone created AND gets activated to use it (one operation, no second visit needed)
- Only **new** doors are touched — existing ones (managed, unmanaged, or deactivated) are left exactly as they are
- Doors REMOVED from Hartmann are not auto-deprovisioned — that's a destructive action and stays manual

Caveat: if you turn this on, then later untick a door from "Create Door Time Zones" to deprovision it, the next hourly sync will add it back. Turn auto-add off if you want a door to stay deprovisioned.

#### Rollback

- **Per-door**: open Door Time Zones, untick the door from "Create Door Time Zones", tick "Apply changes to managed doors on submit", and submit. The integration repoints the door's `DoorTimeZoneId` back to the original (recorded at provisioning time), fires Update Panels, and deletes the HA Door Time Zone.
- **Whole integration**: deleting the integration runs the same cleanup for every managed door automatically. Best-effort — orphaned HA Door Time Zones are tagged with the entry's id in their Description field (`protector_net:<entry_id>:door:<door_id>`) so you can find and remove them manually in Hartmann if anything goes wrong mid-cleanup.

#### Coexistence with `override_door`

The existing `override_door` service is **unchanged**. Keep using it for ad-hoc unlocks and the JS card. The two systems coexist cleanly: an active panel override still wins until cleared, regardless of which schedule sits underneath. Once the override is resumed, the door falls back to whichever schedule it's pointing at — original or HA-managed.

### Panels Online sensor
Each integration entry now exposes a `sensor.panels_online_<partition>` entity on its Hub device. State is the integer count of panels currently online; attributes give the breakdown:

```yaml
state: 1
attributes:
  online_panels:
    - name: Main Panel
      mac: 44B7D0A029D0
      model: PRS-Door-Master
      ip: 192.168.1.42
  offline_panels: []
  online_count: 1
  offline_count: 0
  total_count: 1
  all_online: true
  last_updated: "2026-05-01T13:23:58"
```

Polled every 60 seconds (Hartmann's `/api/PanelCommands/PanelsOnline` endpoint). The MAC → friendly-name + IP map comes from `/api/Panels` and is cached, so the steady-state poll is one cheap API call.

Useful for "notify me if any panel goes offline" automations:

```yaml
trigger:
  - platform: state
    entity_id: sensor.panels_online_default_partition
    attribute: all_online
    to: false
action:
  - service: notify.mobile_app_yourphone
    data:
      title: "Hartmann panel offline"
      message: >-
        Offline:
        {{ state_attr('sensor.panels_online_default_partition', 'offline_panels')
           | map(attribute='name') | join(', ') }}
```

### Partition + door names sync from Hartmann
Renaming a partition or a door in Hartmann is now picked up by HA automatically — no need to delete and re-add the integration, or even reload it.

The integration syncs names on every load and re-checks every hour in the background:

- The hub device picks up new partition names (which also updates the integration entry title and the card's partition section header)
- Door devices get their new names, with entities re-labeled to match
- Worst-case lag between a Hartmann rename and HA picking it up is about an hour

**Custom names you've set in HA are preserved.** If you renamed a door in HA's UI (Settings → Devices → click the door → pencil icon), HA records that as `name_by_user` and the sync skips that device — your custom name wins forever, even when the Hartmann name changes.

This is useful when the Hartmann admin names doors one way (say, internal codes like "Main 4") and you want different labels in HA ("Lobby Door"). Mix and match per-door — let some sync from Hartmann, override others.

**Entity IDs never change** regardless of renames — automations referencing entities by entity_id keep working unconditionally.

### Manage Active PINs view (Hartman Door Lock Card)
A new "Manage Active PINs" panel in the bulk actions area lets you see every active temp code at a glance — name, expiry, how many doors it unlocks, and the PIN itself (hidden behind a "show" toggle by default; flip the new `always_show_temp_pin: true` card option if you'd rather see them all). Clicking a code expands it inline so you can:

- **Add or remove doors** without changing the PIN — checkboxes for every door, one Save click applies the diff. Behind the scenes this hits the new `add_door_to_temp_code` / `remove_door_from_temp_code` services, so the same Hartmann user gets assigned to (or removed from) the new doors' Access Privilege Groups.
- **Extend the expiry** with a datetime picker — same PIN, no interruption to the guest.
- **Delete now** — removes the Hartmann user immediately.

Removing the last door is allowed — the Hartmann user record stays intact (you can re-add doors later or delete it manually from the Hartmann admin UI).

### Multi-door temp codes now actually work
Bulk-creating a PIN for multiple doors at once would previously only succeed for the **first** door — every subsequent door got rejected by Hartmann with “PIN provided is not available. This could indicate the PIN is in use or being blocked by Enhanced Pin Security.” The integration was creating a separate Hartmann user for each door with the same PIN, and Hartmann’s built-in PIN-uniqueness rule blocked all but the first.

The `create_temp_code` service now creates **one** Hartmann user with a single PIN credential and assigns that user to each requested door’s Access Privilege Group. Bulk-creating a code across 6 doors now does what you’d expect: 6 working doors, one user record. `update_temp_code` and `delete_temp_code` now propagate changes/cleanups to every sensor that was tracking the same code, so the per-door temp_code sensors stay in sync.

### Auto-deletion of expired temp codes
Temp codes created with an `end_time` now auto-delete themselves the moment they expire — the Hartmann user record is removed and the entry disappears from the Temp Code sensor. No more 5-minute polling automation needed for cleanup; it just works for hotel/Airbnb-style bookings out of the box. Existing helper automations that delete codes by name still work fine — they’ll just become a redundant safety net.

The scheduler survives Home Assistant restarts: any restored codes whose `end_time` is in the past get cleaned up immediately, and codes still in the future are rescheduled. Updating a code’s `end_time` (e.g., via `update_temp_code` for an extended booking) reschedules the deletion automatically.

If Hartmann is unreachable when a code tries to expire, the integration retries every hour until it succeeds — the entry stays in the sensor until the cleanup actually completes, so nothing gets silently dropped.

### Quieter logs for stale temp codes
The “No temp user found with PIN …” log line is now DEBUG instead of WARNING. This was just the API layer being chatty about a routine, recoverable case (e.g., the user was deleted out-of-band) — the actual error was already returned in the structured response, and callers handle it appropriately.

---



### Fix: WebSocket auto-heals after session expiry
If the Hartmann session cookie expired while the WebSocket was running, the connection would drop and never recover — every reconnect attempt failed silently because it kept using the stale cookie. The WebSocket client now refreshes its credentials on each reconnect attempt, re-authenticates automatically on 401 errors, and shuts down cleanly when Home Assistant stops (no more “Task was still running” warnings in the logs).

### Fix: Last Door Log timestamps showing UTC instead of local time
Event timestamps like “Home Assistant unlocked @ 6:39 AM” were displaying the raw UTC time from the Hartmann server instead of converting to your local timezone. Now correctly shows local time (e.g., “@ 2:39 AM” for EDT).

### Fix: No more phantom activity entries after restart
Restarting or reloading the integration used to create a wall of fake state-change entries in the Activity log (e.g., “Override Type changed to For Specified Time”, “Lock State changed to Locked”) even though nothing actually happened at the door. Sensors now restore silently without triggering history entries.

### Quieter logs during server reboots
Transient connection errors (timeouts, connection refused) during Hartmann server reboots no longer show as errors in the HA log. They’re now logged as warnings since the integration always retries and self-heals.

---

## What’s new in 0.2.3

### Fix: Door sensors missing on some Odyssey servers
On certain Hartmann systems, the overview tree’s site names didn’t match the partition name, causing door discovery to silently find zero doors — only the Hub Status sensor would appear. Discovery now uses the partition’s own door list from the API (the same reliable source the websocket client already uses), with the old site-name filter as a fallback.

### Fix: Override Type resets after restart
The Override Type select (“Until Resumed”, “For Specified Time”, etc.) used to reset to “For Specified Time” every time Home Assistant restarted, because the Hartmann API doesn’t expose the current override type. The select now persists its value across restarts.

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
| `create_temp_code` | Create a temporary PIN code with optional start/end times. Supports random or manual codes, configurable digit count (4–9). Multi-door: creates one user with the PIN, assigns to each requested door's APG. |
| `update_temp_code` | Update start/end time of an existing code without changing the PIN. Perfect for extending guest stays. |
| `add_door_to_temp_code` | Add a door to an existing temp code so the same PIN unlocks one more door. |
| `remove_door_from_temp_code` | Remove a door from a temp code without deleting the user. PIN keeps working on remaining doors. |
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

* **Door sensors missing (only Hub Status appears)**
  If you see “No doors matched filters” in the logs, update to **0.2.3**. Older versions relied on site-name matching which fails on some Odyssey servers. The fix uses partition-scoped door discovery instead.

* **Override Type resets to “For Specified Time” after restart**
  Update to **0.2.3**; the Override Type select now persists across restarts.

* **WebSocket disconnects and never reconnects**
  Update to **0.2.4**. Older versions captured the session cookie once at startup; if it expired, reconnects would fail forever. The WS client now re-authenticates automatically.

* **“Task was still running after final writes shutdown stage” warnings**
  Update to **0.2.4**; the WebSocket tasks now stop cleanly when Home Assistant shuts down.

* **Last Door Log shows wrong time (off by several hours)**
  Update to **0.2.4**. Timestamps from Hartmann are in UTC and were being displayed without timezone conversion.

* **Fake activity entries after every restart/reload**
  Update to **0.2.4**. Sensors now restore their previous state silently without creating history entries.

* **Sensors didn’t appear previously for “Default Partition”**
  Update to **0.1.7**; discovery now correctly loads those doors.

---

## Changelog

### 0.2.5
* New: **Manage Active PINs** panel in the door card — view all active temp codes, add/remove doors per code without changing the PIN, extend expiry, or delete. New card config option `always_show_temp_pin` to skip the show-PIN toggle.
* New: **`add_door_to_temp_code`** and **`remove_door_from_temp_code`** services — extend an existing temp code's reach to additional doors, or revoke from specific doors, without changing the PIN.
* Fix: **Multi-door temp codes** — `create_temp_code` now creates one Hartmann user with multiple APG assignments instead of one user per door, fixing the bulk-create rejection caused by Hartmann’s PIN-uniqueness rule. `update_temp_code` and `delete_temp_code` now broadcast changes to every sensor that tracks the same code so they stay in sync.
* New: **Auto-delete on expiration** — temp codes with an `end_time` now delete themselves automatically (both from Hartmann and from the sensor) the moment they expire. Survives HA restarts; reschedules on `update_temp_code`. Retries every hour if Hartmann is unreachable.
* Improvement: **Quieter logs** — “No temp user found with PIN …” downgraded from WARNING to DEBUG since the structured response already conveys the result.

### 0.2.4
* Fix: **WebSocket auto-reconnect after session expiry** — credentials refresh on each reconnect; negotiate re-authenticates on 401
* Fix: **Clean HA shutdown** — WebSocket tasks stop on EVENT_HOMEASSISTANT_STOP (no more shutdown warnings)
* Fix: **Last Door Log timestamps** now display in local time instead of raw UTC
* Fix: **No phantom activity entries** on restart/reload — sensors restore silently
* Improvement: **Quieter logs** — transient connection errors downgraded from ERROR to WARNING

### 0.2.3
* Fix: **Door sensors missing on some Odyssey servers** — discovery now uses the partition’s API door list instead of fragile site-name matching
* Fix: **Override Type select** now persists across restarts via RestoreEntity

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
