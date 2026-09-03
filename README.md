[![Total Downloads](https://img.shields.io/github/downloads/hitchin999/protector_net/total.svg?label=Total%20Downloads&style=for-the-badge&color=blue)](https://github.com/hitchin999/protector_net/releases)
[![Active Protector.Net Installs][prot-badge]][prot-analytics]

[prot-badge]: https://img.shields.io/badge/dynamic/json?label=Active%20Installs&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.protector_net.total&style=for-the-badge&color=blue
[prot-analytics]: https://analytics.home-assistant.io/integration/protector_net

# Protector.Net & Odyssey Access Control for Home Assistant

This custom integration controls **Hartmann Controls Protector.Net _and_ Odyssey** door access systems via HTTP + a live **SignalR** websocket for instant updates.

---

## What's new in 0.2.7

### Fix: door entities recover on their own after a Hartmann outage

If the integration **(re)started while Hartmann was unreachable** — an HA restart or reload coinciding with a server reboot (e.g. the nightly panel bounce) or a brief network drop — the setup-time door fetch failed and the door platforms came up with **zero door entities**. The integration still finished loading "successfully", so your doors sat **unavailable** with nothing backing them, and **any automation keyed on them silently stopped**, until you manually reloaded. A websocket reconnect only refreshes entities that *already exist*; it can't recreate missing ones.

A successful SignalR **(re)connect is now treated as a recovery signal**. Every door platform re-checks for doors it's missing, **backfills** them, and re-seeds their state from cache so they don't sit at *Unknown*. Your doors come back on their own when Hartmann does. It's a **no-op on a healthy start**, adds **no load while Hartmann is down** (it only fires on a *successful* reconnect), and picks up doors added in Hartmann during the outage as a bonus.

Covers every per-door entity — the Override **Type** / **Mode** selects, **Override Minutes**, **Override Until**, **Pulse Unlock** and optional legacy buttons, the **Lock State** / **Overridden** / **Reader Mode** / **Last Door Log** / **Temp Code** / **OTR** sensors, and the door-contact **binary sensors** — plus the partition-wide **All Doors Lockdown** switch.

### Fix: door entities unavailable on Home Assistant 2026.9+

On Home Assistant **2026.9** and newer, every **Door** entity and the **All Doors Lockdown** switch came up unavailable and stayed that way — a reload didn't help, since it recurred on every start. Hub and Action Plans entities kept working, so it looked like a partial outage. Home Assistant changed how a device links to its parent (the link nesting each Door under its Hub), and the old form began raising an error, which made Home Assistant silently drop those entities.

Doors now link to the Hub the new way on Home Assistant 2026.8+, and the old way on older versions. Device grouping, entity IDs, dashboards, and automations are unchanged, and there's **no minimum Home Assistant version bump**.

### Fix: Reconfigure and re-authentication rebuilt every entity twice

Completing a **Reconfigure** or **re-authentication** reloaded the entry twice, so every entity went unavailable and came back twice in a row. It's now a single clean reload.

**No configuration or automation changes needed — just update.**

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
* ✅ **Self-healing** — door entities auto-recover after a Hartmann outage (no manual reload)
  
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

### Reconfigure and re-authentication (no more delete-and-re-add)
You can now fix credentials in place. The integration's **Reconfigure** flow (Settings → Devices & Services → Protector.Net → ⋮ → Reconfigure) updates the **Protector.Net URL, username, and password** without removing the integration — all entities, options, and HA-managed door schedules are preserved. Point it at a new host and the entry's unique ID and title update automatically; it refuses the change if it would collide with another configured entry/partition.

And when the server rejects the stored credentials (e.g. the Hartmann account password was changed), the integration raises a proper **re-authentication** prompt asking you to re-enter the username and password — instead of failing quietly in the background. Both flows reload the entry cleanly on success.

> Note: this is distinct from the WebSocket session re-auth, which silently refreshes the `ss-id` cookie on reconnect. That handles *expired* sessions; this handles *wrong* credentials.

### Partition + door names sync from Hartmann
Renaming a partition or a door in Hartmann is now picked up by HA automatically — no need to delete and re-add the integration, or even reload it.

The integration syncs names on every load and re-checks every hour in the background:

- The hub device picks up new partition names (which also updates the integration entry title and the card's partition section header)
- Door devices get their new names, with entities re-labeled to match
- Worst-case lag between a Hartmann rename and HA picking it up is about an hour

**Custom names you've set in HA are preserved.** If you renamed a door in HA's UI (Settings → Devices → click the door → pencil icon), HA records that as `name_by_user` and the sync skips that device — your custom name wins forever, even when the Hartmann name changes.

This is useful when the Hartmann admin names doors one way (say, internal codes like "Main 4") and you want different labels in HA ("Lobby Door"). Mix and match per-door — let some sync from Hartmann, override others.

**Entity IDs never change** regardless of renames — automations referencing entities by entity_id keep working unconditionally.

---

## Devices & Entities

### 1) Hub device (per partition)

* **Device:** `Hub Status – <Partition>`
* **Entity:** **Hub Status – <Partition>** *(sensor)*
  **State:** `running / connecting / idle / stopped / error`
  **Attributes:** `phase`, `connected`, `mapped_doors`, `partition_id`, `system_type` *(“Odyssey” or “ProtectorNET”)* 
* **Update Panels** *(button)* — push configuration to all connected panels immediately
* **Panels Online – <Partition>** *(sensor)* — count of panels currently online; attributes break down online/offline panels (name, MAC, model, IP). Polled every 60s.
* **Door Schedules – <Partition>** *(sensor)* — per-door current Door Time Zone (schedule) assignment **as configured on the server**, with `ha_managed` and a lifecycle `status` (Active / Staged / Drifted / Unmanaged) for each door. Lets you see which doors are on their HA-managed schedule without opening each door in Hartmann. Refreshed every 5 min and immediately after `set_door_schedule_mode`.

#### Panels Online attributes

State is the integer count of panels currently online; attributes give the breakdown:

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

#### Door Schedules attributes

State is the number of doors currently on an HA-managed schedule; the `doors` attribute lists every door with its current schedule and a lifecycle status:

```yaml
state: 3
attributes:
  doors:
    - door_id: 5
      name: Front Door
      schedule: "HA[abc1234] Front Door"   # the Door Time Zone the door points at right now
      ha_managed: true
      status: Active                         # door is on its HA-managed schedule
      mode: CardOrPin
    - door_id: 6
      name: Basement Entrance
      schedule: "Always Unlock"
      ha_managed: false
      status: Drifted                        # HA thinks it's active, but it isn't on the HA schedule
      mode: Unlock
  ha_managed_count: 3
  staged_count: 1
  drifted_count: 1
  unmanaged_count: 2
  total_count: 7
  last_updated: "2026-06-19T13:23:58"
```

The four statuses:

- **Active** — door is on its HA-managed schedule, as intended.
- **Staged** — an HA schedule is provisioned for the door, but it hasn't been activated onto it yet (still on its original schedule).
- **Drifted** — `managed_doors` says the door should be active, but on the server its assigned Door Time Zone is **not** the HA one (e.g. someone repointed it in Hartmann directly, or an activation didn't take). Worth investigating.
- **Unmanaged** — HA isn't managing this door's schedule at all.

This reads each door's assignment from the **server** (its `DoorTimeZoneId` resolved against the partition's Door Time Zone list) — the same thing you'd see in Hartmann's door config. Note it reflects the **server's configured schedule, not what the panel hardware is currently enforcing**: if an Update Panels push were ever lost, the server (and this sensor) would still show the intended schedule. To confirm a panel actually applied a mode, compare with the per-door **Reader Mode** sensor (live from the panel). Refreshes every 5 minutes and immediately after a `set_door_schedule_mode` call.

### 2) Door devices (one per door)

**Sensors**

* **Lock State** — `Locked` / `Unlocked`
* **Overridden** — `On` / `Off`
* **Reader Mode** — mapped from controller index:
  `0/8 Lockdown, 1 Card, 2 Pin, 3 Card or Pin, 4 Card and Pin, 5 Unlock, 6 First Credential In, 7 Dual Credential`
* **Last Door Log by** — highlights the last actor with timestamp (e.g., “John Smith granted access @ 1:06 PM”); attributes include the last **reader/action** message/time and the last **door** message.
* **Temp Code** — state is `None` or the active code name. Attributes: list of all temp codes with `code_name`, `code`, `user_id`, `start_time`, `end_time` per entry.
* **OTR Schedules** — state is the count of schedules for this door. Attributes: `active_schedules` (currently running), `upcoming_schedules` (future), and `all_schedules` with id, name, mode, start, and stop times. Refreshes every 5 minutes and immediately after create/delete.
* **Door Contact** *(binary_sensor, `device_class=door`)* — live **Open** / **Closed** state from the panel's door-contact input. Attributes: `contact_configured` (whether a contact input is actually wired) and `held_open` (door propped past its threshold). Doors without a contact input default to **Closed** with `contact_configured: false`.

  Auto-discovered from Hartmann: the integration reads each panel's inputs (`/api/Panels/{id}/Inputs`), finds those configured as `Door_Contact` or `Monitored_Door_Contact`, and maps them to the right door — so the sensor's name is just the door name and HA's logbook/history shows clean Open / Closed transitions. State is driven live off the SignalR/WebSocket feed (`DOOR_CONTACT_STATE` / `DOOR_CONTACT_INPUT_STATE`, already polarity-corrected by Hartmann), on **both Protector.Net and Odyssey** from the same path. `contact_configured` becomes `true` only once a real contact is confirmed — either discovered in the Hartmann config, or proven by a live notification — so automations can branch on it instead of trusting a hardcoded state. `held_open` is reported directly by Odyssey and derived from the contact-state stream on Protector.Net. State and `held_open` survive HA restarts, with the next live notification re-syncing to ground truth.

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
| `set_door_schedule_mode` | Set an **HA-managed** door's schedule mode (Lockdown / Card / Pin / Card or Pin / Card and Pin / Unlock / First Credential In / Dual) for the whole week, 24/7. Persists across panel reboots. Door must first be added under **Options → Door Time Zones**. |
| `update_panels` | Push current configuration to all connected panels immediately. |

All services accept **multiple doors** via the device picker.

Rapid `set_door_schedule_mode` calls coalesce into a **single** Update Panels push, fired after a short quiet window once every Door Time Zone write in the burst has committed — so no door's change can be lost to a competing push. The window defaults to 2.5 s (`UPDATE_PANELS_DEBOUNCE_SECONDS` in `const.py`); a couple of seconds of latency on a scheduled push is invisible in practice. `override_door` / `resume_door` send direct panel commands and are not affected.

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

* **Door entities stuck "unavailable" after a Hartmann outage (automations stopped), fixed only by reloading**
  Update to **0.2.7**. If the integration (re)started while the server was down — e.g. an HA restart or reload during a Hartmann reboot — the door platforms came up empty and the door entities stayed unavailable even after the server returned, because a websocket reconnect can only refresh entities that already exist. The integration now rebuilds the missing door entities automatically on the next successful reconnect.

* **A scheduled door didn't lock/unlock, but there's no error and nothing in the panel log**
  Update to **0.2.6**. When an automation changed several doors within a second or two (e.g. a batch `set_door_schedule_mode` immediately followed by another call for one more door), the second Update Panels push could be dropped while the panel was still applying the first — so that door's change reached the server but never the hardware. Pushes are now coalesced through a debouncer into a single Update Panels fired after all writes land.

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

### 0.2.7
* Fix: **Door entities auto-recover after a server outage** — if the integration (re)started while Hartmann was unreachable (an HA restart / reload coinciding with a server reboot or network drop), the setup-time door fetch failed and the door platforms came up with **no entities**, so the existing door entities went **unavailable** and nothing retried — they stayed stuck (and automations keyed on them stopped) until a manual reload. The integration now treats a successful SignalR reconnect as a recovery signal and **backfills** any missing door entities the moment the websocket comes back, then re-seeds their state. Covers every per-door entity (override selects / Override Minutes / Override Until, Pulse Unlock + legacy buttons, Lock State / Overridden / Reader Mode / Last Log / Temp Code / OTR sensors, door-contact binary sensors) and the All Doors Lockdown switch. No-op on a healthy start; no extra load during the outage; also picks up doors added in Hartmann during the outage. No config/automation changes needed. Complements the 0.2.4 websocket auto-reconnect (which keeps the connection alive — this rebuilds the entities).
* Fix: **Door entities unavailable on Home Assistant 2026.9+** — HA changed how a device links to its parent (the link nesting each Door under its Hub), and the old form began raising an error, so HA silently dropped every entity that used it. All **Door** entities and the **All Doors Lockdown** switch went unavailable and stayed there (a reload didn't help); Hub and Action Plans entities were unaffected. Doors now link to the Hub by registry ID on HA 2026.8+ and the old way on older versions. No minimum HA version bump; grouping, entity IDs, and automations unchanged.
* Fix: **Reconfigure / re-authentication reloaded the entry twice** — every entity went unavailable and came back twice in a row. The update listener now owns the reload, so it's a single clean rebuild.

### 0.2.6
* Fix: **Update Panels race on rapid schedule changes** — back-to-back `set_door_schedule_mode` calls (e.g. a batch lock followed immediately by a second call for one more door) each fired their own `PanelCommands/UpdateAll`; the second could be dropped while the panel was still applying the first, leaving that door's new schedule on the server but never pushed to hardware — with no HA error and no panel log. Pushes now route through a per-entry **debouncer** that coalesces a burst into a single Update Panels fired after all Door Time Zone writes commit, plus a short retry for a transient panel-offline. `override_door` / `resume_door` are unaffected (they use direct panel commands); no automation changes needed.
* New: **Door Schedules sensor** — `sensor.door_schedules_<partition>` on the Hub device lists each door's current Door Time Zone (schedule) assignment from the server and whether it's the HA-managed one (status: Active / Staged / Drifted / Unmanaged), so you don't have to open each door in Hartmann to check. Reflects the server's configured schedule (not panel-enforced state); refreshes every 5 min and immediately after `set_door_schedule_mode`.

### 0.2.5
* New: **HA-managed door schedules** — opt-in per door under **Options → Door Time Zones**. Creates a dedicated Door Time Zone in Hartmann and (optionally) repoints the door to it, so schedule changes survive panel reboots. Rolls back cleanly on untick or integration removal.
* New: **`set_door_schedule_mode`** service — rewrite an HA-managed door's mode (Lockdown/Card/Pin/Card or Pin/Card and Pin/Unlock/First Credential In/Dual) 24/7; auto-pushes to panels. Idempotent.
* New: **Auto-add new doors** toggle — optionally enroll and activate newly-discovered Hartmann doors automatically on the hourly sync (off by default).
* New: **Door contact sensors** — per-door `binary_sensor` (`device_class=door`) auto-discovered from `Door_Contact` / `Monitored_Door_Contact` inputs, with `contact_configured` and `held_open` attributes. Live updates over SignalR; Protector.Net + Odyssey.
* New: **Panels Online sensor** — `sensor.panels_online_<partition>` on the Hub device, with online/offline panel breakdown attributes. Polled every 60s.
* New: **Reconfigure flow** — edit the Protector.Net URL, username, and password in place without removing the integration; entities, options, and schedules preserved. Plus a proper **re-authentication** prompt when the server rejects stored credentials.
* New: **Partition + door name sync** — renames in Hartmann flow through to HA on load and hourly; manually-customized names preserved; entity IDs never change.
* New: **Options menu split** — **Basic Settings** and **Door Time Zones** are now separate pages.
* New: **Manage Active PINs** panel in the door card — view all active temp codes, add/remove doors per code without changing the PIN, extend expiry, or delete. New card config option `always_show_temp_pin`.
* New: **`add_door_to_temp_code`** and **`remove_door_from_temp_code`** services — extend or revoke a temp code's door reach without changing the PIN.
* Fix: **Multi-door temp codes** — `create_temp_code` now creates one Hartmann user with multiple APG assignments instead of one user per door, fixing the bulk-create rejection from Hartmann’s PIN-uniqueness rule. `update_temp_code` / `delete_temp_code` broadcast to every sensor tracking the same code.
* New: **Auto-delete on expiration** — temp codes with an `end_time` delete themselves (Hartmann + sensor) when they expire. Survives HA restarts; reschedules on `update_temp_code`; retries hourly if Hartmann is unreachable.
* Improvement: **More resilient deletion** — a 400 from the user-delete endpoint is verified against actual user existence and treated as success if the user is already gone, killing spurious retry loops.
* Improvement: **Quieter logs** — stale-PIN lookups downgraded to DEBUG; auto-expire retry downgraded to INFO; removed the noisy job-listener `list.remove(x)` traceback on options reload.

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
